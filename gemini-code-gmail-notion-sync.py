import os
import re
import json
import base64
import datetime
import traceback
from email.utils import parseaddr

import requests
from google import genai

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ==========================================
# 1. 설정
# ==========================================
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not NOTION_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError(
        "❌ 환경변수 NOTION_TOKEN / GEMINI_API_KEY 가 설정되지 않았습니다. "
        "setx로 등록 후 '새' 터미널을 열어서 실행하세요."
    )

DB_ID_PROJECT  = "3b97f1a743518017a679c8a89b49c862"  # 프로젝트 DB
DB_ID_DOCUMENT = "3b97f1a7435180d9a34ae8b5b081c575"  # 문서 DB
DB_ID_TASK     = "3b97f1a7435180ceaf72cba94cc612c8"  # 작업 DB
DB_ID_MEETING  = "3b97f1a743518093b885d4facbac2dee"  # 회의 DB
DB_ID_MEMO     = "3b97f1a74351808a80d5ffb2a329862d"  # 메모 DB

NOTION_PAGES_URL = "https://api.notion.com/v1/pages"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

ai_client = genai.Client(api_key=GEMINI_API_KEY)

MY_NAME = "설용환"

TARGET_LABEL_PREFIX = "INBOX/메가존/고객사 문의/"

CREDENTIALS_FILE = r"C:\temp\credentials.json"
TOKEN_FILE = r"C:\temp\token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# ==========================================
# 2. Notion DB 스키마 - 실행 시점에 동적으로 조회
# ==========================================
_SCHEMA_CACHE = {}

def fetch_schema(db_id):
    if db_id in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[db_id]
    url = f"https://api.notion.com/v1/databases/{db_id}"
    res = requests.get(url, headers=NOTION_HEADERS)
    res.raise_for_status()
    properties = res.json().get("properties", {})
    schema = {name: prop.get("type") for name, prop in properties.items()}
    _SCHEMA_CACHE[db_id] = schema
    return schema

def get_title_property(schema):
    for name, ptype in schema.items():
        if ptype == "title":
            return name
    return None

def find_property(schema, candidates, allowed_types):
    for cand in candidates:
        if cand in schema and schema[cand] in allowed_types:
            return cand
    return None

def build_title(text):
    return {"title": [{"text": {"content": text[:2000]}}]}

def build_richtext_long(text, max_total=6000):
    text = (text or "내용 없음")[:max_total]
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or ["내용 없음"]
    return {"rich_text": [{"text": {"content": c}} for c in chunks]}

def extract_richtext(prop_value):
    parts = prop_value.get("rich_text", []) if prop_value else []
    return "".join(p.get("plain_text", "") for p in parts)

def build_status_or_select(ptype, name):
    if ptype == "status":
        return {"status": {"name": name}}
    if ptype == "select":
        return {"select": {"name": name}}
    return None

def build_date(date_str):
    return {"date": {"start": date_str}}

def build_relation(page_id):
    return {"relation": [{"id": page_id}]}

def build_paragraph_blocks(text, max_total=8000):
    text = (text or "내용 없음")[:max_total]
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or ["내용 없음"]
    return [
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [{"type": "text", "text": {"content": c}}]}}
        for c in chunks
    ]

def append_children(page_id, blocks):
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    res = requests.patch(url, headers=NOTION_HEADERS, json={"children": blocks})
    if res.status_code != 200:
        print(f"      ⚠️ [페이지 본문 추가 실패] {res.status_code}: {res.text[:300]}")
    return res.status_code == 200

def print_schema(label, schema):
    print(f"   🧩 [{label} DB] 감지된 속성: " + ", ".join(f"{k}({v})" for k, v in schema.items()))

def build_full_content(short_text, body, max_body=1500):
    body_text = (body or "").strip()
    if len(body_text) > max_body:
        body_text = body_text[:max_body] + "\n...(이하 본문 생략)"
    parts = []
    if short_text and short_text.strip():
        parts.append(short_text.strip())
    if body_text:
        parts.append("─── 메일 원문 ───\n" + body_text)
    return "\n\n".join(parts) if parts else "내용 없음"

def normalize_subject(subject):
    s = (subject or "").strip()
    prefix_pattern = re.compile(r'^(re|fw|fwd|회신|전달|답장)\s*[:：]\s*', re.IGNORECASE)
    prev = None
    while prev != s:
        prev = s
        s = prefix_pattern.sub('', s).strip()
    s = re.sub(r'^\[(EXT|EXTERNAL)\]\s*', '', s, flags=re.IGNORECASE).strip()
    return s

def norm_loose(text):
    """공백 무시 + 소문자화 (회사명/제목 느슨한 비교용)"""
    return re.sub(r'\s+', '', (text or '')).lower()

# ==========================================
# 2-1. 진행 상황 저장/재개 (API 사용량 제한 등으로 중단돼도 이어서 처리)
# ==========================================
PROGRESS_FILE = r"C:\temp\sync_progress.json"

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("processed_message_ids", []))
        except Exception as e:
            print(f"   ⚠️ 진행 상황 파일 읽기 실패(새로 시작): {e}")
    return set()

def save_progress(processed_ids):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"processed_message_ids": list(processed_ids)}, f, ensure_ascii=False)
    except Exception as e:
        print(f"   ⚠️ 진행 상황 저장 실패: {e}")

RATE_LIMIT_MARKERS = ["resource_exhausted", "rate limit", "429", "quota"]

def is_rate_limit_error(e):
    return any(marker in str(e).lower() for marker in RATE_LIMIT_MARKERS)

# ==========================================
# 3. Gmail 인증
# ==========================================
def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔑 Gmail 토큰 갱신 중...")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise RuntimeError(
                    f"❌ {CREDENTIALS_FILE} 이 없습니다. Google Cloud Console에서 "
                    "OAuth 클라이언트(데스크톱 앱) JSON을 받아 해당 경로에 저장하세요."
                )
            print("🔑 최초 인증 - 브라우저가 열립니다. 구글 계정으로 로그인/승인해주세요.")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        print(f"   💾 토큰 저장 완료: {TOKEN_FILE}")

    return build("gmail", "v1", credentials=creds)

# ==========================================
# 4. 라벨(=회사 폴더) 탐색
# ==========================================
def get_company_labels(service):
    results = service.users().labels().list(userId="me").execute()
    labels = results.get("labels", [])
    print(f"   ℹ️ 전체 라벨 {len(labels)}개 중 '{TARGET_LABEL_PREFIX}' 하위 탐색...")

    company_labels = {}
    for label in labels:
        name = label["name"]
        if name.startswith(TARGET_LABEL_PREFIX):
            company_name = name[len(TARGET_LABEL_PREFIX):]
            if company_name and "/" not in company_name:
                company_labels[company_name] = label["id"]

    return company_labels

def list_all_messages(service, label_id):
    """읽음 여부와 무관하게 라벨의 전체 메일 ID를 페이지네이션으로 모두 수집"""
    all_refs = []
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId="me", labelIds=[label_id], pageToken=page_token, maxResults=100
        ).execute()
        all_refs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return all_refs

# ==========================================
# 5. 메일 파싱 유틸
# ==========================================
def get_message_body(payload):
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []) or []:
        body = get_message_body(part)
        if body:
            return body

    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        html = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
        return re.sub("<[^<]+?>", " ", html)

    return ""

def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""

# ==========================================
# 6. 노션 조회 / 중복판별 함수
# ==========================================
def get_existing_projects(title_prop):
    projects = {}
    query_url = f"https://api.notion.com/v1/databases/{DB_ID_PROJECT}/query"
    payload = {"page_size": 100}
    while True:
        res = requests.post(query_url, headers=NOTION_HEADERS, json=payload)
        res.raise_for_status()
        data = res.json()
        for page in data.get("results", []):
            title_parts = page.get("properties", {}).get(title_prop, {}).get("title", [])
            name = "".join(t.get("plain_text", "") for t in title_parts)
            if name:
                projects[name] = page["id"]
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return projects

def get_recent_documents(project_id, project_prop, title_prop, content_prop, limit=100):
    if not project_prop:
        return []
    query_url = f"https://api.notion.com/v1/databases/{DB_ID_DOCUMENT}/query"
    payload = {
        "filter": {"property": project_prop, "relation": {"contains": project_id}},
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": limit
    }
    res = requests.post(query_url, headers=NOTION_HEADERS, json=payload)
    if res.status_code != 200:
        return []

    docs = []
    for page in res.json().get("results", []):
        props = page.get("properties", {})
        title_parts = props.get(title_prop, {}).get("title", [])
        title = "".join(t.get("plain_text", "") for t in title_parts)
        content = extract_richtext(props.get(content_prop)) if content_prop else ""
        if title:
            docs.append({"id": page["id"], "title": title, "content": content})
    return docs

def get_existing_titles(db_id, title_prop, project_prop, project_id, limit=100):
    if not project_prop or not title_prop:
        return []
    query_url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {
        "filter": {"property": project_prop, "relation": {"contains": project_id}},
        "page_size": limit
    }
    res = requests.post(query_url, headers=NOTION_HEADERS, json=payload)
    if res.status_code != 200:
        return []
    titles = []
    for page in res.json().get("results", []):
        parts = page.get("properties", {}).get(title_prop, {}).get("title", [])
        t = "".join(p.get("plain_text", "") for p in parts)
        if t:
            titles.append(t)
    return titles

def match_existing_document(subject, recent_docs):
    if not recent_docs:
        return None

    normalized_new = normalize_subject(subject).strip()
    for d in recent_docs:
        t_wo_date = re.sub(r'^\[\d{4}-\d{2}-\d{2}\]\s*', '', d["title"])
        if normalize_subject(t_wo_date).strip() == normalized_new:
            return d

    listing = "\n".join(f"{i}: {d['title']}" for i, d in enumerate(recent_docs[:20]))
    prompt = f"""새 메일 제목: {subject}

아래는 같은 프로젝트에 이미 등록된 최근 문서 제목 목록이야 (번호: 제목):
{listing}

새 메일이 위 목록 중 하나와 같은 건(예: 같은 문의에 대한 회신/전달 스레드)이면 그 번호만 숫자로 답해.
새로운 별개의 건이면 "NONE" 이라고만 답해. 다른 설명 없이 숫자 또는 NONE만 반환해."""

    try:
        response = ai_client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
        result = response.text.strip()
        if result.isdigit() and int(result) < len(recent_docs):
            return recent_docs[int(result)]
    except Exception as e:
        print(f"      ⚠️ 스레드 매칭 AI 오류(새 문서로 처리): {e}")

    return None

def check_incremental_info(existing_content, new_body):
    prompt = f"""기존에 정리된 문서 내용:
{(existing_content or '')[:2500]}

새로 도착한 메일(같은 스레드의 답장) 원문:
{(new_body or '')[:2500]}

질문: 새 메일이 위 기존 내용에 없던 새로운 정보(답변, 진행상황, 결정사항, 요청 변경, 일정 등)를 담고 있는가?
단순 "확인했습니다", "감사합니다" 같은 인사/승인 외 실질적 정보가 없으면 새 정보 없음으로 판단해.

응답은 반드시 아래 JSON 구조만 (다른 텍스트 금지):
{{"has_new_info": true 또는 false, "incremental_summary": "새로운 정보만 2~5문장으로 요약 (없으면 빈 문자열)"}}"""

    try:
        response = ai_client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        print(f"      ⚠️ 신규 정보 판별 AI 오류(안전하게 '신규 정보 있음'으로 처리): {e}")
        return {"has_new_info": True, "incremental_summary": new_body[:500] if new_body else ""}

def is_duplicate_item(new_title, new_detail, existing_titles, kind_label):
    """작업/회의/메모를 만들기 전에, 이미 같은 프로젝트에 등록된 것과 동일한지 확인"""
    if not existing_titles:
        return False

    norm_new = norm_loose(new_title)
    for t in existing_titles:
        norm_t = norm_loose(t)
        if norm_new == norm_t or (len(norm_new) > 4 and norm_new in norm_t) or (len(norm_t) > 4 and norm_t in norm_new):
            return True

    listing = "\n".join(f"- {t}" for t in existing_titles[:30])
    prompt = f"""새로 추가하려는 {kind_label} 제목: {new_title}
내용: {new_detail or ''}

이미 등록되어 있는 {kind_label} 목록:
{listing}

새 항목이 위 목록 중 하나와 사실상 같은 내용이면 "DUPLICATE", 새로운 내용이면 "NEW" 라고만 답해. 다른 설명 없이 그 단어만 반환해."""

    try:
        response = ai_client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
        return response.text.strip().upper().startswith("DUPLICATE")
    except Exception as e:
        print(f"      ⚠️ {kind_label} 중복 판별 AI 오류(중복 아님으로 간주): {e}")
        return False

# ==========================================
# 7. AI 스마트 분석 함수
# ==========================================
def analyze_content_types(subject, body):
    prompt = f"""
    너는 스마트한 업무 비서야. 아래 이메일을 분석해서 Notion에 기록할 내용을 만들어줘.

    [1순위 - 문서 요약 및 다음 액션]
    먼저 이 메일 전체를 읽고 아래 두 가지를 작성해:
    - summary: 요청 배경, 핵심 요청사항, 관련 담당자, 언급된 일정 등을 이 요약 하나만 읽어도
      파악할 수 있도록 5~10문장으로 정리
    - next_action: 이 메일에 대해 다음에 취해야 할 구체적인 행동을 한 문장으로 작성

    [2순위 - 위 요약 내용에 근거해서만 판단]
    아래는 위에서 정리한 문서 내용에 실제로 해당하는 경우에만 채워. 애매하거나 근거가 약하면
    억지로 채우지 말고 빈 리스트로 둬.

    1. 작업 (tasks): 내가 직접 실행/행동해야 하는 구체적인 할 일.
       회의 일정을 조율하거나 잡아야 하는 경우도 '작업'으로 분류해 (회의로 넣지 마).
       각 항목: {{"title": "작업명", "detail": "무엇을, 어떻게, 왜 해야 하는지 2~3문장", "due_date": "YYYY-MM-DD 또는 null"}}
    2. 회의 (meetings): 아래 두 경우에만 포함해. '일정 조율이 필요하다'는 이유만으로는 절대 넣지 마 (그건 작업으로 분류).
       - 이미 회의가 진행되었고 그 결과/내용이 메일에 공유된 경우 → 상태 [회의록/완료]
       - 다음 회의 일시가 이미 확정/합의된 경우 → 상태 [진행 예정]
       상태를 title 앞에 [대괄호]로 표기.
       각 항목: {{"title": "[상태] 회의명", "detail": "회의 결과 또는 확정된 일정 등 구체적 내용 2~3문장", "meeting_date": "YYYY-MM-DD 또는 null"}}
    3. 메모 (memos): 오직 계정 정보(로그인 정보, 발급된 계정, 권한, 접속 URL 등) 또는
       주소 정보(사무실 주소, 배송지, 방문지 등)에 해당하는 내용일 때만 만들어.
       그 외 일반 참고사항/정책/히스토리는 메모로 만들지 마.
       각 항목: {{"title": "메모 제목", "detail": "계정/주소 관련 구체적 정보 1~2문장"}}
    4. 유형 (type): '프로세스', '교육', '미팅', '메인' 중 1개 선택.

    메일 제목: {subject}
    메일 본문: {body[:3000]}

    응답은 반드시 아래 JSON 구조만 (```json 등 마크다운 기호, 다른 설명 텍스트 금지):
    {{
        "summary": "문서 요약 내용",
        "next_action": "다음 액션 한 문장",
        "type": "분류된 유형",
        "tasks": [{{"title": "...", "detail": "...", "due_date": null}}],
        "meetings": [{{"title": "...", "detail": "...", "meeting_date": null}}],
        "memos": [{{"title": "...", "detail": "..."}}]
    }}
    (해당하는 내용이 없으면 빈 리스트 [] 로 반환)
    """
    try:
        response = ai_client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        print(f"   ⚠️ AI 분석 오류: {e}")
        return {"summary": "", "next_action": "", "type": "메인", "tasks": [], "meetings": [], "memos": []}

# ==========================================
# 8. 노션 DB 등록/수정 함수들 (스키마 기반 동적 매핑)
# ==========================================
CONTENT_CANDIDATES = ["내용", "설명", "상세", "상세내용", "메모", "비고", "Detail"]
DATE_CANDIDATES = ["일자", "날짜", "수신일자"]
DUE_DATE_CANDIDATES = ["마감일", "기한", "완료예정일", "마감", "Due Date"]
MEETING_DATE_CANDIDATES = ["회의일자", "회의일시", "미팅일자", "일정", "날짜"]
REQUESTER_CANDIDATES = ["요청자", "발신자", "보낸사람"]
PROJECT_CANDIDATES = ["프로젝트"]
NEXT_ACTION_CANDIDATES = ["다음액션", "다음 액션", "Next Action", "액션"]

def create_document(doc_title, body, sender_name, mail_date, doc_type, project_id, summary="", next_action=""):
    """성공 시 새 문서의 page_id, 실패 시 None 반환"""
    schema = fetch_schema(DB_ID_DOCUMENT)
    title_prop = get_title_property(schema) or "이름"
    properties = {title_prop: build_title(doc_title)}

    status_prop = find_property(schema, ["상태"], ["status", "select"])
    if status_prop:
        val = build_status_or_select(schema[status_prop], "진행 중")
        if val:
            properties[status_prop] = val

    content_prop = find_property(schema, CONTENT_CANDIDATES, ["rich_text"])
    full_content = build_full_content(summary, body)
    if content_prop:
        properties[content_prop] = build_richtext_long(full_content)

    requester_prop = find_property(schema, REQUESTER_CANDIDATES, ["rich_text"])
    if requester_prop:
        properties[requester_prop] = build_richtext_long(sender_name, max_total=200)

    date_prop = find_property(schema, DATE_CANDIDATES, ["date"])
    if date_prop:
        properties[date_prop] = build_date(mail_date)

    kind_prop = find_property(schema, ["종류"], ["select", "status"])
    if kind_prop:
        val = build_status_or_select(schema[kind_prop], "메인")
        if val:
            properties[kind_prop] = val

    type_prop = find_property(schema, ["유형"], ["select", "status"])
    if type_prop:
        val = build_status_or_select(schema[type_prop], doc_type)
        if val:
            properties[type_prop] = val

    next_action_prop = find_property(schema, NEXT_ACTION_CANDIDATES, ["rich_text"])
    if next_action_prop and next_action:
        properties[next_action_prop] = build_richtext_long(next_action, max_total=500)

    project_prop = find_property(schema, PROJECT_CANDIDATES, ["relation"])
    if project_prop:
        properties[project_prop] = build_relation(project_id)

    payload = {
        "parent": {"database_id": DB_ID_DOCUMENT},
        "properties": properties,
        "children": build_paragraph_blocks(full_content)
    }
    res = requests.post(NOTION_PAGES_URL, headers=NOTION_HEADERS, json=payload)
    if res.status_code != 200:
        print(f"      ⚠️ [문서 생성 실패] {res.status_code}: {res.text[:300]}")
        return None
    return res.json()["id"]

def update_document_with_new_reply(page_id, content_prop, date_prop, existing_content, incremental_summary, new_body, mail_date):
    """성공 시 병합된 전체 내용 문자열, 실패해도 페이지 본문 추가는 시도"""
    appended = f"\n\n─── 후속 메일 ({mail_date}) ───\n{incremental_summary.strip()}\n\n{(new_body or '').strip()[:1500]}"
    merged = (existing_content or "").strip() + appended

    properties = {}
    if content_prop:
        properties[content_prop] = build_richtext_long(merged, max_total=8000)
    if date_prop:
        properties[date_prop] = build_date(mail_date)

    if properties:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        res = requests.patch(url, headers=NOTION_HEADERS, json={"properties": properties})
        if res.status_code != 200:
            print(f"      ⚠️ [문서 업데이트 실패] {res.status_code}: {res.text[:300]}")

    append_children(page_id, build_paragraph_blocks(appended.strip()))
    return merged

def create_task(title, project_id, body, detail=None, due_date=None):
    schema = fetch_schema(DB_ID_TASK)
    title_prop = get_title_property(schema) or "이름"
    properties = {title_prop: build_title(title)}

    status_prop = find_property(schema, ["상태"], ["status", "select"])
    if status_prop:
        val = build_status_or_select(schema[status_prop], "다음 퀘스트")
        if val:
            properties[status_prop] = val

    content_prop = find_property(schema, CONTENT_CANDIDATES, ["rich_text"])
    full_content = build_full_content(detail, body)
    if content_prop:
        properties[content_prop] = build_richtext_long(full_content)

    due_prop = find_property(schema, DUE_DATE_CANDIDATES, ["date"])
    if due_prop and due_date:
        properties[due_prop] = build_date(due_date)

    project_prop = find_property(schema, PROJECT_CANDIDATES, ["relation"])
    if project_prop:
        properties[project_prop] = build_relation(project_id)

    payload = {
        "parent": {"database_id": DB_ID_TASK},
        "properties": properties,
        "children": build_paragraph_blocks(full_content)
    }
    res = requests.post(NOTION_PAGES_URL, headers=NOTION_HEADERS, json=payload)
    if res.status_code != 200:
        print(f"      ⚠️ [작업 생성 실패] {res.status_code}: {res.text[:300]}")

def create_meeting(title, project_id, body, detail=None, meeting_date=None):
    schema = fetch_schema(DB_ID_MEETING)
    title_prop = get_title_property(schema) or "이름"
    now_str = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    properties = {title_prop: build_title(title)}

    created_prop = find_property(schema, ["생성 일시"], ["date"])
    if created_prop:
        properties[created_prop] = build_date(now_str)

    content_prop = find_property(schema, CONTENT_CANDIDATES, ["rich_text"])
    full_content = build_full_content(detail, body)
    if content_prop:
        properties[content_prop] = build_richtext_long(full_content)

    meeting_date_prop = find_property(schema, [c for c in MEETING_DATE_CANDIDATES if c != "생성 일시"], ["date"])
    if meeting_date_prop and meeting_date_prop != created_prop and meeting_date:
        properties[meeting_date_prop] = build_date(meeting_date)

    project_prop = find_property(schema, PROJECT_CANDIDATES, ["relation"])
    if project_prop:
        properties[project_prop] = build_relation(project_id)

    payload = {
        "parent": {"database_id": DB_ID_MEETING},
        "properties": properties,
        "children": build_paragraph_blocks(full_content)
    }
    res = requests.post(NOTION_PAGES_URL, headers=NOTION_HEADERS, json=payload)
    if res.status_code != 200:
        print(f"      ⚠️ [회의 생성 실패] {res.status_code}: {res.text[:300]}")

def create_memo(title, project_id, body, detail=None):
    schema = fetch_schema(DB_ID_MEMO)
    title_prop = get_title_property(schema) or "이름"
    properties = {title_prop: build_title(title)}

    content_prop = find_property(schema, CONTENT_CANDIDATES, ["rich_text"])
    full_content = build_full_content(detail, body)
    if content_prop:
        properties[content_prop] = build_richtext_long(full_content)

    project_prop = find_property(schema, PROJECT_CANDIDATES, ["relation"])
    if project_prop:
        properties[project_prop] = build_relation(project_id)

    payload = {
        "parent": {"database_id": DB_ID_MEMO},
        "properties": properties,
        "children": build_paragraph_blocks(full_content)
    }
    res = requests.post(NOTION_PAGES_URL, headers=NOTION_HEADERS, json=payload)
    if res.status_code != 200:
        print(f"      ⚠️ [메모 생성 실패] {res.status_code}: {res.text[:300]}")

# ==========================================
# 9. 메인 자동화 프로세스 (전체 메일 ↔ 노션 대조 방식)
# ==========================================
def main():
    print("🔄 [Gmail ➔ 노션 통합 DB] 전체 메일 대조 동기화 시작... (시간이 걸릴 수 있습니다)")

    processed_ids = load_progress()
    if processed_ids:
        print(f"ℹ️ 이전 실행에서 처리 완료된 메일 {len(processed_ids)}건 기록 확인, 이어서 진행합니다.")

    for label, db_id in [("프로젝트", DB_ID_PROJECT), ("문서", DB_ID_DOCUMENT),
                          ("작업", DB_ID_TASK), ("회의", DB_ID_MEETING), ("메모", DB_ID_MEMO)]:
        try:
            schema = fetch_schema(db_id)
            print_schema(label, schema)
        except Exception as e:
            print(f"   ⚠️ [{label} DB] 스키마 조회 실패: {e}")

    doc_schema = fetch_schema(DB_ID_DOCUMENT)
    doc_title_prop = get_title_property(doc_schema) or "이름"
    doc_project_prop = find_property(doc_schema, PROJECT_CANDIDATES, ["relation"])
    doc_content_prop = find_property(doc_schema, CONTENT_CANDIDATES, ["rich_text"])
    doc_date_prop = find_property(doc_schema, DATE_CANDIDATES, ["date"])

    task_schema = fetch_schema(DB_ID_TASK)
    task_title_prop = get_title_property(task_schema) or "이름"
    task_project_prop = find_property(task_schema, PROJECT_CANDIDATES, ["relation"])

    meeting_schema = fetch_schema(DB_ID_MEETING)
    meeting_title_prop = get_title_property(meeting_schema) or "이름"
    meeting_project_prop = find_property(meeting_schema, PROJECT_CANDIDATES, ["relation"])

    memo_schema = fetch_schema(DB_ID_MEMO)
    memo_title_prop = get_title_property(memo_schema) or "이름"
    memo_project_prop = find_property(memo_schema, PROJECT_CANDIDATES, ["relation"])

    project_title_prop = get_title_property(fetch_schema(DB_ID_PROJECT)) or "이름"

    service = get_gmail_service()

    company_labels = get_company_labels(service)
    if not company_labels:
        print(f"❌ '{TARGET_LABEL_PREFIX}' 하위 라벨을 찾지 못했습니다. Gmail 라벨 이름/구조를 확인해주세요.")
        return

    print(f"✅ 대상 회사 라벨 {len(company_labels)}개 발견: {list(company_labels.keys())}")

    existing_projects = get_existing_projects(project_title_prop)
    print(f"ℹ️ 노션 프로젝트 DB에 등록된 회사 {len(existing_projects)}개: {list(existing_projects.keys())}")

    normalized_projects = {}
    for pname, pid in existing_projects.items():
        normalized_projects.setdefault(norm_loose(pname), (pname, pid))

    matched = {}
    skipped = []
    for label_name, label_id in company_labels.items():
        key = norm_loose(label_name)
        if key in normalized_projects:
            matched_project_name, project_id = normalized_projects[key]
            matched[label_name] = (label_id, project_id)
            if matched_project_name != label_name:
                print(f"   ℹ️ 이름 차이 있지만 매칭됨: Gmail '{label_name}' ↔ 노션 프로젝트 '{matched_project_name}'")
        else:
            skipped.append(label_name)

    if skipped:
        print(f"⏭️ 노션 프로젝트에 없어 건너뛰는 라벨: {skipped}")
    if not matched:
        print("❌ 노션 프로젝트 DB와 이름이 일치하는 Gmail 라벨이 하나도 없습니다.")
        return

    for company_name, (label_id, project_id) in matched.items():
        try:
            all_refs = list_all_messages(service, label_id)
        except Exception as e:
            print(f"   ⚠️ [{company_name}] 라벨 조회 실패: {e}")
            continue

        if not all_refs:
            print(f"📂 [{company_name}] 라벨 - 메일 없음")
            continue

        full_msgs = []
        for ref in all_refs:
            try:
                msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
                full_msgs.append(msg)
            except Exception as e:
                print(f"   ⚠️ 메일 조회 실패({ref.get('id')}): {e}")
        full_msgs.sort(key=lambda x: int(x.get("internalDate", 0)))  # 오래된 메일부터 처리 (스레드 순서 보존)

        to_process = [m for m in full_msgs if m["id"] not in processed_ids]
        already_done = len(full_msgs) - len(to_process)

        if already_done:
            print(f"\n📂 [{company_name}] 라벨 - 전체 {len(full_msgs)}건 중 {already_done}건은 이전 실행에서 처리 완료, {len(to_process)}건 남음")
        if not to_process:
            continue

        print(f"📂 [{company_name}] 라벨 - {len(to_process)}건 노션과 대조 시작")

        recent_docs = get_recent_documents(project_id, doc_project_prop, doc_title_prop, doc_content_prop, limit=100)
        existing_task_titles = get_existing_titles(DB_ID_TASK, task_title_prop, task_project_prop, project_id)
        existing_meeting_titles = get_existing_titles(DB_ID_MEETING, meeting_title_prop, meeting_project_prop, project_id)
        existing_memo_titles = get_existing_titles(DB_ID_MEMO, memo_title_prop, memo_project_prop, project_id)

        for msg in to_process:
            try:
                headers = msg["payload"].get("headers", [])
                subject = get_header(headers, "Subject") or "제목 없음"
                from_header = get_header(headers, "From")
                sender_name, sender_email = parseaddr(from_header)
                sender_name = sender_name or sender_email or MY_NAME

                body = get_message_body(msg["payload"])
                mail_date = datetime.datetime.fromtimestamp(int(msg["internalDate"]) / 1000).strftime("%Y-%m-%d")
                doc_title = f"[{mail_date}] {subject}"

                matched_doc = match_existing_document(subject, recent_docs)

                if matched_doc:
                    verdict = check_incremental_info(matched_doc["content"], body)
                    if not verdict.get("has_new_info"):
                        print(f"   ⏭️ 같은 스레드, 새 정보 없어 건너뜀: {doc_title}")
                        processed_ids.add(msg["id"])
                        save_progress(processed_ids)
                        continue

                    incremental_summary = verdict.get("incremental_summary", "")
                    print(f"   🔄 같은 스레드, 새 정보 있어 기존 문서 업데이트: {matched_doc['title']}")
                    merged = update_document_with_new_reply(
                        matched_doc["id"], doc_content_prop, doc_date_prop,
                        matched_doc["content"], incremental_summary, body, mail_date
                    )
                    if merged:
                        matched_doc["content"] = merged

                    ai_data = analyze_content_types(subject, incremental_summary + "\n\n" + (body or ""))
                else:
                    print(f"   ✉️ 신규 문서 생성: {doc_title}")
                    ai_data = analyze_content_types(subject, body)
                    doc_type = ai_data.get("type", "메인")
                    summary = ai_data.get("summary", "")
                    next_action = ai_data.get("next_action", "")

                    new_page_id = create_document(doc_title, body, sender_name, mail_date, doc_type, project_id,
                                                   summary=summary, next_action=next_action)
                    if not new_page_id:
                        print(f"      └ ❌ 문서 생성 실패로 하위 작업/회의/메모 생성 건너뜀: {doc_title}")
                        continue

                    recent_docs.insert(0, {"id": new_page_id, "title": doc_title,
                                            "content": build_full_content(summary, body)})

                for task in ai_data.get("tasks", []):
                    title = task.get("title", "제목 없음") if isinstance(task, dict) else str(task)
                    detail = task.get("detail") if isinstance(task, dict) else None
                    if is_duplicate_item(title, detail, existing_task_titles, "작업"):
                        print(f"      └ ⏭️ [작업 중복 판단, 건너뜀]: {title}")
                        continue
                    due_date = task.get("due_date") if isinstance(task, dict) else None
                    create_task(title, project_id, body, detail=detail, due_date=due_date)
                    existing_task_titles.append(title)
                    print(f"      └ 📌 [작업 생성]: {title}")

                for meeting in ai_data.get("meetings", []):
                    title = meeting.get("title", "제목 없음") if isinstance(meeting, dict) else str(meeting)
                    detail = meeting.get("detail") if isinstance(meeting, dict) else None
                    if is_duplicate_item(title, detail, existing_meeting_titles, "회의"):
                        print(f"      └ ⏭️ [회의 중복 판단, 건너뜀]: {title}")
                        continue
                    meeting_date = meeting.get("meeting_date") if isinstance(meeting, dict) else None
                    create_meeting(title, project_id, body, detail=detail, meeting_date=meeting_date)
                    existing_meeting_titles.append(title)
                    print(f"      └ 📅 [회의 생성]: {title}")

                for memo in ai_data.get("memos", []):
                    title = memo.get("title", "제목 없음") if isinstance(memo, dict) else str(memo)
                    detail = memo.get("detail") if isinstance(memo, dict) else None
                    if is_duplicate_item(title, detail, existing_memo_titles, "메모"):
                        print(f"      └ ⏭️ [메모 중복 판단, 건너뜀]: {title}")
                        continue
                    create_memo(title, project_id, body, detail=detail)
                    existing_memo_titles.append(title)
                    print(f"      └ 📝 [메모 생성]: {title}")

                print("      └ ✅ 처리 완료")
                processed_ids.add(msg["id"])
                save_progress(processed_ids)

            except Exception as e:
                if is_rate_limit_error(e):
                    print(f"\n🛑 API 사용량 제한에 걸린 것 같습니다: {e}")
                    print(f"   지금까지 처리된 메일 {len(processed_ids)}건은 저장했습니다.")
                    print(f"   {PROGRESS_FILE} 기록을 바탕으로, 잠시 후(또는 내일) 다시 실행하면 이어서 처리됩니다.")
                    save_progress(processed_ids)
                    return
                print(f"   ❌ 메일 처리 중 오류: {e}")
                traceback.print_exc()

    print("\n🎉 모든 동기화 작업이 완료되었습니다!")

if __name__ == "__main__":
    main()
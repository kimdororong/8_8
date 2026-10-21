"""UI/UX 디자인 피드백 · 컨펌 봇 (표준 라이브러리만 사용).

실행:  ANTHROPIC_API_KEY=sk-ant-... python server.py   →  http://localhost:8765
"""
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("REVIEW_MODEL", "claude-sonnet-5-5")
PORT = int(os.environ.get("PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 12 * 1024 * 1024

CRITERIA = {
    "usability": "사용성/UX 휴리스틱: 시각적 위계, 일관성, 사용자 흐름, 피드백, 오류 예방 (Nielsen 10 휴리스틱 기준)",
    "a11y": "접근성(WCAG 2.1 AA): 색 대비(텍스트 4.5:1, 큰 글씨 3:1), 터치 영역 44px 이상, 글자 크기, 색에만 의존한 정보 전달",
    "system": "디자인 시스템 준수: 사용자가 준 규칙(색·폰트·간격·컴포넌트)과 일치하는지, 임의 값이 섞여 있는지",
    "copy": "UX 카피: 버튼/에러/빈 화면 문구의 명확성, 톤 일관성, 행동 유도",
}

VERDICTS = ("approved", "conditional", "changes_requested")

SYSTEM = """당신은 시니어 UI/UX 디자인 리뷰어입니다. 주어진 화면 이미지를 보고 디자인 컨펌 여부를 판정합니다.
- 이미지에서 실제로 보이는 것만 근거로 삼고, 추측은 '추정'이라고 표시하세요.
- 각 이슈에는 화면의 어느 부분인지(location), 무엇이 문제인지(problem), 어떻게 고칠지(fix)를 구체적으로 쓰세요.
- severity: blocker(출시 전 반드시 수정) / major(수정 권장) / minor(다듬기).
- verdict: blocker가 있으면 changes_requested, blocker 없이 major가 있으면 conditional, 그 외 approved.
- 좋은 점(strengths)도 반드시 1~3개 적으세요.
- 한국어로, 아래 JSON만 출력하세요(코드 펜스·설명 금지):
{"verdict":"approved|conditional|changes_requested","summary":"한두 문장 총평","strengths":["..."],
 "issues":[{"severity":"blocker|major|minor","category":"usability|a11y|system|copy","location":"...","problem":"...","fix":"..."}]}"""


def build_prompt(context, criteria, rules):
    lines = ["이 화면을 리뷰해 주세요.", "", "평가 기준:"]
    for key in criteria:
        lines.append(f"- {CRITERIA[key]}")
    if context:
        lines += ["", f"화면 설명/목적: {context}"]
    if rules:
        lines += ["", "우리 팀 디자인 규칙:", rules]
    return "\n".join(lines)


def parse_review(text):
    """모델 응답에서 JSON을 꺼내 형식을 보정한다. 실패하면 ValueError."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못했어요")
    data = json.loads(m.group(0))
    issues = [i for i in data.get("issues", []) if isinstance(i, dict)]
    for i in issues:
        if i.get("severity") not in ("blocker", "major", "minor"):
            i["severity"] = "minor"
    # 판정은 이슈 심각도로 다시 계산해서 모델이 일관성 없게 답해도 규칙을 지킨다
    if any(i["severity"] == "blocker" for i in issues):
        verdict = "changes_requested"
    elif any(i["severity"] == "major" for i in issues):
        verdict = "conditional"
    else:
        verdict = "approved"
    return {
        "verdict": verdict,
        "summary": str(data.get("summary", "")),
        "strengths": [str(s) for s in data.get("strengths", [])],
        "issues": issues,
    }


def split_data_url(data_url):
    m = re.match(r"data:(image/(?:png|jpeg|webp|gif));base64,(.+)$", data_url, re.S)
    if not m:
        raise ValueError("PNG/JPEG/WebP/GIF 이미지만 올릴 수 있어요")
    return m.group(1), m.group(2)


def call_claude(image_data_url, context, criteria, rules):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 환경변수가 없어요")
    media_type, b64 = split_data_url(image_data_url)
    body = {
        "model": MODEL,
        "max_tokens": 2500,
        "system": SYSTEM,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": build_prompt(context, criteria, rules)},
            ],
        }],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API 오류 {e.code}: {e.read().decode(errors='replace')[:300]}")
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return parse_review(text)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/review":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("content-length", 0))
            if n > MAX_BODY:
                return self._send(413, {"error": "이미지가 너무 커요 (약 8MB 이하)"})
            req = json.loads(self.rfile.read(n))
            criteria = [c for c in req.get("criteria", []) if c in CRITERIA] or list(CRITERIA)
            result = call_claude(req.get("image", ""), req.get("context", "").strip(),
                                 criteria, req.get("rules", "").strip())
            self._send(200, result)
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})
        except RuntimeError as e:
            self._send(502, {"error": str(e)})


if __name__ == "__main__":
    print(f"디자인 리뷰 봇: http://localhost:{PORT}  (모델: {MODEL})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

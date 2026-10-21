# 디자인 컨펌 봇

UI/UX 스크린샷을 올리면 Claude가 사용성·접근성(WCAG)·디자인 시스템 준수·UX 카피 기준으로 피드백하고
**컨펌 / 조건부 컨펌 / 수정 후 재요청** 중 하나로 판정해요.

```
ANTHROPIC_API_KEY=sk-ant-... python server.py   # http://localhost:8765
python -m unittest test_server                  # 테스트
```

- 판정은 이슈 심각도로 서버가 다시 계산해요: 필수 수정(blocker)이 있으면 수정 요청, 수정 권장(major)만 있으면 조건부 컨펌, 없으면 컨펌.
- 팀 디자인 규칙을 입력창에 붙여 넣으면 그 규칙 기준으로 검사해요.
- 모델은 `REVIEW_MODEL` 환경변수로 바꿀 수 있어요. 설치할 패키지는 없어요(Python 3.8+).

# 외부 스킬 후보 검토와 파이프라인 적용안

검토일: 2026-10-26 · 방법: 웹으로 원문/레포 페이지 확인 (코드 설치·실행은 하지 않음)
관련 문서: [outsourcing_ai_workflow.md](outsourcing_ai_workflow.md)

## 1. 검토 결과 요약

| 후보 | 확인 수준 | 라이선스 | 판단 |
|---|---|---|---|
| anthropics/skills `frontend-design` | **원문 확인** | Apache 2.0 (LICENSE.txt 참조) | ✅ 채택 추천 |
| Owl-Listener/designer-skills | 레포 페이지 확인 | MIT | ✅ 채택 추천 (일부만) |
| borghei/Claude-Skills `design-auditor` | 레포 페이지만 확인, **해당 스킬은 확인 못 함** | MIT + Commons Clause | ⚠️ 보류 |
| Impeccable, Taste, UI/UX Pro Max, Interface Design 등 | 확인 못 함 | 미확인 | ⏸ 필요 시 별도 검토 |

별 개수는 출처마다 달라서 믿지 않는다. 직접 확인한 값: designer-skills 3.1k, borghei/Claude-Skills 976.
앞서 전달한 "accessibility-audit 1.8k" 수치와 스킬 이름은 **집계 사이트 정보였고 레포에서 확인되지 않았다** (아래 3번).

## 2. `frontend-design` (anthropics/skills)

원문 기준 요약:
- 스크립트·외부 호출 없음. 지시문(SKILL.md)만 있는 스킬이라 위험이 낮다.
- 핵심 내용: 주제(제품·타깃·핵심 과업)에서 디자인을 도출, 히어로 섹션 우선, 타이포그래피로 개성 표현, 장식용 라벨/구조 지양, 모션은 절제, 카피도 디자인 요소, 토큰 계획 → 자기 검토 → 제작, AI 디자인 클리셰 회피, 반응형·포커스·모션 감소 등 품질 하한선, 자기 비평.
- 적용 단계: **시안 제작(단계 6)**. 시안 생성 프롬프트의 기본 지침으로 사용.
- 한계: 규칙을 주는 스킬이라 생성 도구가 따로 필요하고, 고객 브랜드 가이드가 있으면 그쪽이 우선이다.
- 우리 쪽 보강: "토큰 계획 → 브리프와 대조 → 제작" 순서를 고객 컨펌본 브리프(단계 3)와 연결한다.

## 3. Owl-Listener/designer-skills

레포 페이지 기준:
- MIT, 3.1k 별, 111 스킬·34 명령·9 플러그인(디자인 실무 컬렉션). 설치: `/plugin marketplace add Owl-Listener/designer-skills`
- 플러그인: design-research, design-systems, ux-strategy, ui-design, interaction-design, prototyping-testing, design-ops, designer-toolkit, visual-critique
- **`accessibility-audit`라는 이름은 페이지에서 찾지 못했다.** 비슷한 것: `/design-systems:audit-system`, `/prototyping-testing:evaluate`(휴리스틱 평가), `visual-critique` 플러그인. 접근성 전용 스킬은 별도 컬렉션(inclusive design)일 수 있다.
- `scripts` 디렉터리가 있으나 내용은 확인하지 못함 → 설치 전 확인 필요.

우리 파이프라인과 매핑:
| 우리 단계 | 후보 플러그인 |
|---|---|
| 리서치 | design-research |
| 정보구조·UX 전략 | ux-strategy |
| 스타일/토큰 | design-systems, ui-design |
| 검수 | visual-critique, prototyping-testing(evaluate) |
| 납품·운영 | design-ops, designer-toolkit |

전부 설치하지 말고 **단계별로 필요한 플러그인 1~2개만** 켜서 비교한다 (스킬이 많으면 AI가 엉뚱한 스킬을 고를 수 있다).

## 4. borghei/Claude-Skills `design-auditor`

- 레포는 MIT + **Commons Clause**: 오픈소스·개인·교육·내부 업무 용도는 무료, 유료 제품으로 판매·재배포는 불가. 외주 작업에 내부 도구로 쓰는 것은 대체로 가능해 보이나 **우리 사용 방식이 허용 범위인지 라이선스 원문으로 확인**해야 한다.
- 파이썬 도구 906개(표준 라이브러리만 사용)를 포함 → 실행 코드가 있으므로 설치 전 코드 검토 필수.
- `design-auditor`가 실제로 존재하는지, 무엇을 검사하는지는 **확인하지 못했다.** 확인 전까지 보류.

## 5. 적용안 (파이프라인 연결)

1. **시안 제작**: `frontend-design` 지침 + 고객 브리프/토큰을 입력으로 사용.
2. **검수**: 우리 검수 봇의 규칙·근거 구조를 기본으로 하고, designer-skills의 `visual-critique`/`evaluate`를 AI 판단 영역의 참고 프롬프트로 활용. 수치 검사(대비·크기)는 코드로 직접 구현.
3. **리서치·IA·스타일**: designer-skills의 해당 플러그인 중 1개씩 시범 적용.
4. **직접 만들어야 하는 영역**: 문의 분석/견적, SOW, 피드백 분해·범위 판정, 핸드오프 문서 (인기 스킬이 확인되지 않음).

## 6. 설치 전 체크리스트

- [ ] SKILL.md 전체와 포함된 스크립트를 읽었는가
- [ ] 라이선스가 상업 외주 사용을 허용하는가 (Commons Clause 같은 제한 확인)
- [ ] 최근 커밋·이슈 상태가 건강한가
- [ ] 한 프로젝트에 시범 적용 후 결과를 비교했는가 (스킬 적용 전/후)
- [ ] 고객 자료를 외부로 보내는 동작이 없는가

## 7. 다음 액션

1. `frontend-design`을 시안 제작 프롬프트에 넣어 같은 브리프로 적용 전/후 결과 비교
2. designer-skills에서 `visual-critique`, `design-systems`만 먼저 설치해 결과 확인
3. `design-auditor`는 레포에서 위치와 코드를 확인한 뒤 결정
4. 확인하지 못한 후보(Impeccable, Taste, UI/UX Pro Max)는 필요할 때 SKILL.md를 읽고 판단

# 깃발 대항전 작업 폴더

2026 연고전 AI 해커톤 「깃발 대항전」 봇 개발 폴더입니다. 규칙은 `rules/해커톤.pdf`에 있습니다.

## 폴더 구조

```text
koyeon/
├── rules/              대회 운영 규칙 + 게임 규칙서 (해커톤.pdf, 추출 텍스트·이미지)
├── dev_tools/          공식 엔진과 대전·검사 도구
│   ├── battle.py       연세 봇 vs 고려 봇 대전 CLI (아래 사용법)
│   ├── tests/          Codex 봇 전략 회귀 테스트
│   ├── engine/ runner/ mapgen/ config/ docs/   대회 제공 원본
│   └── bots/dist/starter/                      대회 제공 스타터 킷 (예제 봇, 제출 도구)
├── out/                봇 코드
│   ├── py_sample/      대회 제공 Python 샘플 (원본 그대로)
│   ├── cpp_sample/     대회 제공 C++ 샘플 (원본 그대로)
│   └── codex_bot/      Codex가 만든 제출용 봇, 제출 ZIP, 검증 기록
└── site/               규칙·전략 설명 사이트 (index.html을 브라우저로 열기)
```

## 대전 CLI

`koyeon` 폴더에서 실행합니다. 연세(Y) 봇, 고려(K) 봇 순서로 적습니다.

```bash
python3 dev_tools/battle.py codex lv2                      # 시드 0 맵에서 한 판
python3 dev_tools/battle.py codex lv2 --seeds 0-9 --swap   # 맵 10개 × 진영 교체 = 20판
python3 dev_tools/battle.py out/codex_bot/koyeon-bot.zip cpp --replay out/replays
python3 dev_tools/battle.py --list                         # 봇 별칭 목록
```

봇은 별칭이나 경로로 지정합니다.

| 별칭 | 대상 |
| --- | --- |
| `codex` | `out/codex_bot` (Codex 봇) |
| `py` | `out/py_sample/main.py` |
| `cpp` | `out/cpp_sample` (자동 컴파일) |
| `lv1`, `lv2` | 공식 Python 예제 |
| `lv1-cpp`, `lv2-cpp` | 공식 C++ 예제 |

경로는 `.py` 파일, `.cpp` 파일, `submission.json`이 있는 폴더, 제출 `.zip`을 받습니다. ZIP은 공식 규격 검사를 거친 뒤 풀어서 실행합니다. C++는 C++20으로 컴파일해 `dev_tools/.build/`에 캐시합니다.

| 옵션 | 뜻 |
| --- | --- |
| `--seeds 0 7 42`, `--seeds 0-9` | 맵 시드. 기본 0 |
| `--swap` | 시드마다 진영을 바꾼 경기도 추가 |
| `--replay DIR` | 리플레이 JSON 저장 |
| `--json FILE` | 전체 결과 저장 |
| `--max-turns N` | 테스트용 턴 상한 |
| `-j N` | N경기 동시 실행. 빠르지만 응답 시간 측정이 부풀 수 있음 |

대회와 같은 시간 제한(첫 턴 3000ms, 이후 300ms)과 몰수 규칙을 적용합니다. 엔진이 조용히 무시하는 잘못된 명령 줄과 출력량 초과도 공식 검사 도구로 찾아 경기마다 표시합니다. 서버의 메모리·CPU 격리는 재현하지 않습니다. 대회 사이트의 하·중·상 봇은 배포되지 않아 로컬에서는 쓸 수 없습니다.

## 그 밖의 명령

```bash
# Codex 봇 전략 테스트 (11개)
python3 -m unittest discover -s dev_tools/tests -v

# 제출 ZIP 만들기와 검사
python3 dev_tools/bots/dist/starter/make_submission.py --source out/codex_bot --output out/codex_bot/koyeon-bot.zip
python3 dev_tools/bots/dist/starter/run_tests.py --zip out/codex_bot/koyeon-bot.zip
```

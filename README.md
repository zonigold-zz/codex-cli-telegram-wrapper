# Codex CLI Telegram Wrapper v2.0.1

Codex CLI의 app-server를 사용하는 단일 운영자용 Telegram 클라이언트입니다.
기존 Codex 로그인과 프로젝트 권한을 사용하며, 별도 OpenAI API 키는 필요하지 않습니다.
Windows, Codex CLI 0.166.0, Python 3.11/3.13에서 검증했습니다. Python 3.10+가 필요합니다.

## 시작하기

1. Codex CLI를 설치하고 `codex login`을 완료합니다. `codex`와 Python이 PATH에 있어야 합니다.
2. `python -m pip install -r requirements.txt`로 의존성을 설치합니다.
3. `.env.example`을 `.env`로 복사하고 봇 토큰, 허용 사용자 ID, 채팅 ID를 입력합니다.
4. 저장소에서 `codex-tg.cmd --tg-workdir "C:\Projects\my-project"`를 실행합니다.

```powershell
python codex_tg_bridge.py --tg-workdir "C:\Projects\my-project" -m gpt-6.1-sol
python codex_tg_bridge.py --tg-workdir "C:\Projects\my-project" --tg-health
python codex_tg_bridge.py --tg-workdir "C:\Projects\my-project" --tg-stop
```

기본적으로 그룹의 포럼 토픽을 사용합니다. 기존 토픽을 쓰려면 `.env`에
`TELEGRAM_THREAD_ID`를 지정합니다. 개인 채팅 또는 토픽을 쓰지 않는 채팅에서는
`--tg-no-topic`을 명시합니다. 한 봇 토큰은 한 브릿지 프로세스에서만 사용하세요.

`--tg-env`, `--tg-state-dir`, `--tg-codex-bin`, `--tg-check-config`를 지원합니다.
상태는 기본적으로 사용자 Documents의 `Codex/codex-telegram-state/프로젝트명`에 저장됩니다.
같은 이름의 프로젝트가 여러 곳에 있으면 각각 다른 `--tg-state-dir`를 지정하세요.
Windows 실행기에 `CODEX_TG_PYTHON`을 지정하면 해당 Python을 사용합니다.

여러 프로젝트가 런타임을 공유할 때만 실행기 옆의 로컬 전용
`.codex_tg_deployment.json`에 절대 경로의 `code_root`, `state_dir`를 지정합니다.
`code_root`는 `codex_telegram` 폴더의 상위 디렉터리입니다.
선택적 `launch_env`에는 비밀값이 아닌 프로젝트 도구·캐시 환경을 설정할 수 있습니다.
`.env`의 설정이 운영체제의 같은 이름 값보다 우선하며, 봇 토큰과 설정 파일의
비밀값은 Codex 하위 프로세스에 전달하지 않습니다.

## 모델과 추론 강도

`/model` 또는 `/model help`는 현재 서버가 제공하는 모델과 각 모델의 지원 강도를
버튼·예시와 함께 보여줍니다. high, max, ultra는 추론 강도이며 모델마다 지원 값이 다릅니다.

```text
/model gpt-6.1-sol
/model gpt-6.1-sol high
/model high
/model max
/model ultra
/effort high
/model cancel
```

실제 서버가 지원하는 값만 적용합니다. `/model high`는 모델을 유지하고 강도만 바꿉니다.
잘못된 모델·강도·추가 인수를 입력하면 설정을 그대로 두고 전체 도움말을 표시합니다.
메뉴를 연 뒤 5분 동안은 모델 ID나 강도만 보내도 선택할 수 있습니다. 이때 잘못된
일반 문장은 Codex 작업으로 실행하지 않습니다. `/model cancel` 또는 닫기 버튼으로
선택을 종료합니다. 다른 작업 명령을 사용해도 모델 선택 대기가 종료됩니다.
모델·강도는 다음 요청부터 적용되며, 기존 대화와 선택은 재시작 후에도 유지됩니다.

## Telegram 명령

| 명령 | 동작 |
|---|---|
| `/status`, `/log`, `/help` | 현재 작업, 연결·대화 상태, 최근 로그, 도움말 |
| `/new`, `/threads`, `/resume 대화ID` | 새 대화, 같은 프로젝트 대화 목록·재개 |
| `/plan 목표`, `/plan show` | 검증 명령을 포함한 계획 초안 작성·열람 |
| `/auto on`, `/resume` | 검토한 계획의 현재 단계부터 자동 진행 |
| `/pause`, `/auto off` | 현재 단계 후 자동 진행 대기 |
| `/cancel` | 현재 턴·검증 중단 요청, 예약 취소, 자동 진행 정지 |
| `/suggest` | 후속 작업 제안. 실행 버튼을 선택할 때 접수 |
| `/answer 답변` | 대기 중인 질문에 자유 답변 |
| `/remember 질문 \| 답변` | 정확히 같은 질문에 사용할 답변 저장 |
| `/prompt_begin`, `/prompt_end`, `/prompt_cancel` | 긴 프롬프트 수집·실행·폐기 |
| `/autoresearch 지시` | 설치된 `$codex-autoresearch` 지시 전달 |

작업 중 새 메시지는 현재 턴에 반영하거나 다음 작업에 예약할 수 있습니다.
모든 입력과 버튼에는 운영자·채팅·토픽 제한이 적용됩니다. 버튼은 한 번만 처리하며,
과거 실행의 승인 버튼으로 새 작업을 승인할 수 없습니다.

## 계획에 따른 자동 진행

`/plan`과 후속 제안은 기존 차단 규칙을 상속하는 임시 읽기 전용 프로필에서 작성합니다.
검토 후 `/auto on`을 선택해야 계획의 해시·범위가 승인됩니다. 각 단계가 끝나면
계획에 적힌 검증 명령을 Codex의 실제 `command/exec` 권한으로 실행합니다.
검증이 통과해야 다음 단계로 넘어갑니다. 계획 안의 일상적 계속 진행 질문은 기록을 남기고
처리하며, 실제 실행·파일·새 권한 승인은 설정된 Codex 검토 절차를 따릅니다.

기본 한도는 8턴, 3시간, 서버 보고 사용량 200,000토큰이며 같은 근거의 진전 없는 반복은
3회에서 멈춥니다. `--tg-max-turns`, `--tg-max-hours`, `--tg-max-tokens`로 조절합니다.
토큰 한도는 서버가 사용량을 보고할 때 검사하며 사전 금액 상한을 보장하지 않습니다.
승인 검토기 `CODEX_TG_APPROVALS_REVIEWER`의 기본값은 `user`이며 `auto_review`도 지원합니다.
프로젝트 권한·검색·추가 쓰기 경로는 기존 Codex 설정으로 관리합니다.
exec 전용 인수인 `--full-auto`, `--search`는 이 실행기에 전달하지 마세요.

## 연결과 복구

- 지시는 SQLite에 저장한 뒤 Telegram offset을 갱신하며 작업 ID로 중복 실행을 막습니다.
- 수신·전송·Codex 실행을 분리하고, 연결 오류·서버 오류·429 대기 시간을 구분해 재시도합니다.
- 미전송 결과, 토픽, 대화, 계획, 모델·강도는 재시작 후 복구합니다.
- OS 잠금으로 같은 봇의 중복 실행을 막습니다. 401/403/409는 인증·권한·다른 수신기를 확인하세요.
- 비정상 종료 당시 실행 중이던 작업은 확인 대기로 보존하며 임의로 재실행하지 않습니다.
- 전송 시간 초과 뒤 결과 메시지가 중복 전달될 수 있습니다. 메시지 재전송으로 작업을 재실행하지 않습니다.
- 토픽 생성 결과가 불명확하면 중복 생성을 멈춥니다. 알려진 토픽 ID나 `--tg-no-topic`으로 해결하세요.

실제 `.env`, 배포 설정, 상태 DB, 로그, 프로젝트 자료는 Git에 올리지 마세요.
이 저장소에는 브릿지 소스·테스트와 실행 안내만 포함합니다.

## 테스트

```powershell
$env:PYTHONPATH = (Resolve-Path .\bridge_v2).Path
python -B -m unittest discover -s .\bridge_v2\tests -v
```

제한된 Windows 환경에서는 `CODEX_TG_TEST_TMPDIR`에 쓰기 가능한 시험 폴더를 지정합니다.
84개 검사는 네트워크나 모델 호출 없이 상태 복구, 권한, 재시도, 계획, 취소,
가짜 app-server와의 파이프 통신, 모델 도움말과 잘못된 입력 처리를 확인합니다.

공식 규격: [Codex app-server](https://learn.chatgpt.com/docs/app-server)

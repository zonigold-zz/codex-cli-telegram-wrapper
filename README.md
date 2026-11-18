# Codex CLI Telegram Wrapper v2.1.1

Codex CLI의 app-server를 사용하는 단일 운영자용 Telegram 클라이언트입니다.
기존 Codex 로그인과 프로젝트 권한을 사용하며, 별도 OpenAI API 키는 필요하지 않습니다.
Windows, Codex CLI 0.166.0, Python 3.11/3.13에서 검증했습니다. Python 3.11+가 필요합니다.

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
처리합니다. 실행 승인은 never로 고정해 새 권한 요청은 자동 거부하고, 기존 권한으로 수행할 수 없는 작업은 원인을 보존해 blocked로 보고합니다.

기본 한도는 8턴, 3시간, 서버 보고 사용량 200,000토큰이며 같은 근거의 진전 없는 반복은
3회에서 멈춥니다. `--tg-max-turns`, `--tg-max-hours`, `--tg-max-tokens`로 조절합니다.
토큰 한도는 서버가 사용량을 보고할 때 검사하며 사전 금액 상한을 보장하지 않습니다.
추가 권한 허용 버튼은 제공하지 않으며 이전 실행의 허용 버튼도 권한을 부여할 수 없습니다.
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
164개 검사는 네트워크나 모델 호출 없이 상태 복구, 권한, 재시도, 계획, 취소,
가짜 app-server와의 파이프 통신, 모델 도움말과 잘못된 입력 처리를 확인합니다.

공식 규격: [Codex app-server](https://learn.chatgpt.com/docs/app-server)

## v2.0.2 복구와 검증

기획·후속 제안은 현재 대화 이력을 복사한 임시 대화에서 생성하고, 작업 후 구독을 해제합니다.
기본 권한 프로젝트는 기본 읽기 전용 프로필을 사용합니다. 사용자 정의 권한이 있는 프로젝트는
기존 비밀 파일·원본 보호 규칙을 상속합니다. 기획 단계의 미실행 조건을 구현 scope로 복사하지
않으며, `/auto on` 후 원래 프로젝트 권한 안에서 승인된 계획을 실행합니다.

다른 Codex 연결이 대화를 점유하면 같은 프로젝트인지 확인합니다. 진행 중인 턴이 없으면
저장된 이력을 복사한 새 대화로 이어가고 원래 대화는 보존합니다. 다른 작업이 진행 중이거나
재개 요청의 결과가 불명확하면 중복 실행하지 않습니다.

실패 시 상세 원인을 Telegram과 `/status`에 표시합니다. 만료된 버튼 응답 확인은 실제 작업
실패로 취급하지 않으며, 버튼의 사용자·채팅·토픽 제한과 단일 실행 처리는 유지됩니다.

실제 검증에는 대화 점유 충돌 복구, 제안 버튼 생성·실행, 모델 변경, 생성된 2단계 계획의
자동 실행과 검증 명령, 중단·재시작, 읽기 전용 및 원본·비밀 파일 차단을 포함했습니다.

## v2.1.1: 독립 CLI와 관리자 창 없는 실행

각 봇의 상태 폴더 아래 `codex-home/`을 독립 CODEX_HOME으로 사용하며 SQLite·로그도
해당 경로로 고정합니다. 데스크톱 IPC·MCP·앱·플러그인·알림·원격 제어와 연결하지 않습니다.
기존 브릿지 소유 대화 하나의 완료된 이력만 이전하며 원본 대화는 보존합니다.
진행 중인 작업은 완료 후 전환하세요. 로그인 갱신 파일을 덮어쓰지 않고 승인 규칙의
변경·삭제·이름 변경은 검토·병합 전까지 시작을 중단합니다.

Windows에서는 `windows.sandbox="mxc"`를 명시해 MXC만 사용합니다. 이 설정을 지원하는
Codex CLI와 MXC 지원 장치가 필요하며 0.166.0에서 검증했습니다. 준비되지 않았거나
정책상 차단되면 작업을 시작하지 않고 원인을 보고합니다. elevated/unelevated로
자동 전환하거나 Windows 관리자 허용창을 띄워 설치·갱신하지 않습니다.
`--tg-setup`도 준비 상태 확인만 수행합니다. `prefer_mxc`의 fallback은 사용하지 않습니다.

독립 홈과 그 안의 프로필, 모든 대화 생성·재개·턴 실행에 `approval_policy="never"`를
적용합니다. 기존 파일시스템·원본·비밀 경로·네트워크 제한은 유지합니다. 권한이
부족하면 허용 버튼이나 반복 질문 없이 기존 범위의 대안을 찾거나 blocked로 보고합니다.
필요한 업무 정보 질문과 검토한 계획의 `/auto on` 시작은 계속 지원합니다.

하위 Codex CLI도 같은 독립 홈을 상속해야 합니다. 프로젝트 자체에서 Windows 방식을
elevated로 재정의했다면 그 항목을 mxc로 바꾸거나 하위 CLI에도 `-c windows.sandbox="mxc"`
를 명시하세요. 긴 작업은 전경 실행 세션을 유지하고 진행 상태를 조회하세요. MXC는
전경 명령이 끝나면 남은 자식 프로세스를 정리하므로 분리된 백그라운드 실행은 피하세요.

기존 관리자 방식은 공용 Windows 계정과 홈별 인증 파일을 사용해 여러 홈의 반복 설정이
서로 충돌할 수 있습니다. MXC는 해당 계정·ACL 설정 경로를 사용하지 않습니다.
공용 샌드박스 계정·서비스를 아직 쓰는 데스크톱 클라이언트가 있다면 유지하세요.
`codex sandbox uninstall`을 개별 브릿지의 임시 폴더 정리에 사용하지 마세요.

`codex-home/`에는 로그인·대화·보안 캐시가 있으므로 커밋하지 마세요.
Windows 규격: [공식 문서](https://learn.chatgpt.com/docs/windows/windows-sandbox).

검증: Python 3.11·3.13 각각 164개 검사(실제 심볼릭 링크 생성 1개는 Windows 권한상
생략, 모의 차단 검사는 통과), 실제 E·F 격리·파일·네트워크 보호·모델 변경·계획 자동
진행·검증·취소·재시작, Telegram 응답 전달과 데스크톱 DB 분리를 확인했습니다.
계획 게시 직후 시작 경쟁 조건과 중복 자동 시작도 회귀 검사합니다.

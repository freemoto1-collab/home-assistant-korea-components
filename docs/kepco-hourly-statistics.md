# 한전(KEPCO) 시간별 사용량·잉여량 통계 — 설치/백업/복원 안내

## 무엇이 바뀌었나
- 한전 파워플래너 `POST /low/real/real_day/usage.do` (`{"SEARCH_DT": "YYYY-MM-DD"}`)로 시간별 수전 사용량(`F_AP_QT`)과 잉여량(역송, `F_AP_QT_REV`)을 조회합니다.
- 한전 표의 "N시"는 (N-1)시~N시 구간입니다. 통계는 그 구간의 **시작 시각**에 기록되어 에너지 대시보드 막대가 한전 표와 같은 시각에 찍힙니다.
- 새 통계(기존 센서/고유 ID는 그대로):
  - `korea_incubator:kepco_<고객번호>_hourly_usage` (kWh, 수전 사용량)
  - `korea_incubator:kepco_<고객번호>_hourly_export` (kWh, 잉여량)
- 최근 3일(재시작 직후 14일)을 30분 간격으로 다시 조회해 같은 시각을 덮어쓰므로 중복이 없고, 한전이 늦게 올린 시간대도 다음 조회에서 채워집니다.
- 에너지 대시보드 → 전력망 소비에 `..._hourly_usage`, 전력망으로 반환에 `..._hourly_export`를 지정하세요.

## 1. 원본 백업 (HAOS)
Samba: `\<HA주소>\config\custom_components\korea_incubator` 폴더를 PC에 복사.

SSH(Terminal & SSH 애드온):
```sh
cd /config/custom_components
cp -a korea_incubator korea_incubator.bak-$(date +%Y%m%d)
```
※ 백업 폴더는 `custom_components` 밖(예: `/config/backup_kepco/20261124/`)에 두는 편이 안전합니다. 안에 두면 HA가 별도 통합으로 읽으려 할 수 있습니다.
```sh
mkdir -p /config/backup_kepco/$(date +%Y%m%d)
cp -a /config/custom_components/korea_incubator /config/backup_kepco/$(date +%Y%m%d)/
```

## 2. 수정본 설치
1. 이 저장소의 fork 브랜치를 받습니다(PC):
   ```sh
   git clone -b freemoto1-collab/kepco-integration-update https://github.com/freemoto1-collab/home-assistant-korea-components.git
   ```
   이미 받았다면 `git fetch origin && git checkout freemoto1-collab/kepco-integration-update && git pull`.
2. `custom_components/korea_incubator` 폴더를 HA의 `/config/custom_components/korea_incubator`에 **통째로 교체**합니다.
   - Samba: 기존 폴더 삭제 후 복사.
   - SSH(PC에서): `scp -r custom_components/korea_incubator root@<HA주소>:/config/custom_components/` (먼저 기존 폴더 삭제: `rm -rf /config/custom_components/korea_incubator`).
   - HA에서 직접 git 사용 시: `git clone -b freemoto1-collab/kepco-integration-update <fork URL> /tmp/kc && rm -rf /config/custom_components/korea_incubator && cp -a /tmp/kc/custom_components/korea_incubator /config/custom_components/`
3. 설정 → 시스템 → 재시작(전체 재시작).
4. 개발자 도구 → 통계에서 위 두 `statistic_id`가 생겼는지 확인합니다. 로그에 `KEPCO hourly statistics sync failed`가 없어야 합니다.

## 3. 문제 시 복원
```sh
rm -rf /config/custom_components/korea_incubator
cp -a /config/backup_kepco/<날짜>/korea_incubator /config/custom_components/
```
(Samba는 백업 폴더를 같은 위치로 되돌림) 후 HA 재시작.
이미 기록된 `..._hourly_*` 통계는 남습니다. 지우려면 개발자 도구 → 통계에서 해당 항목을 삭제하세요.

## Docker 설치(HA Container)용
설정 폴더(`config`)가 호스트 디렉터리이고 파일 소유자가 root/컨테이너 사용자라 호스트에서 직접 복사하면 sudo가 필요할 수 있습니다. 아래는 `docker exec`/`docker cp`를 쓰므로 sudo 없이(docker 권한만 있으면) 됩니다. 컨테이너 이름이 `homeassistant`가 아니면 바꿔 쓰세요.

> 주의: 명령 블록을 복사할 때 프롬프트 줄(`$`, `#`, `PS>` 등)은 복사하지 마세요. 명령어만 붙여넣습니다.

백업(원본, 설치 전에 먼저):
```sh
mkdir -p ./backup/$(date +%Y%m%d)
docker cp homeassistant:/config/custom_components/korea_incubator ./backup/$(date +%Y%m%d)/
```

수정본 설치(`<클론>`은 git clone한 저장소 경로):
```sh
docker exec homeassistant rm -rf /config/custom_components/korea_incubator
docker cp <클론>/custom_components/korea_incubator homeassistant:/config/custom_components/
docker restart homeassistant
```

복원(문제 시):
```sh
docker exec homeassistant rm -rf /config/custom_components/korea_incubator
docker cp ./backup/<날짜>/korea_incubator homeassistant:/config/custom_components/
docker restart homeassistant
```

## 고객번호 표시/다중 고객번호
- **번호 노출 제거**: 기기·항목 이름과 통계 이름은 "한전", "한전 시간별 사용량/잉여량"처럼 번호 없이 표시됩니다. "고객번호" 센서 값은 뒤 4자리만 보입니다(예: `******1234`). 기존 `unique_id`와 `statistic_id`(번호 포함)는 바뀌지 않아 쌓인 데이터와 에너지 대시보드 지정이 유지됩니다. 기존 항목의 표시 이름은 업데이트 후 HA를 재시작(또는 항목 다시 로드)하면 갱신됩니다.
- **새 센서(시간별)**: "최근 집계 시간 사용량", "최근 집계 시간 잉여량"(한전 표에서 값이 있는 가장 최근 시간의 값), "오늘 잉여량 누계". 속성 `집계 구간 시작`/`집계 구간 종료`에 어떤 시간의 값인지 표시됩니다. 센서는 5분마다, 통계는 30분마다 최근 3일을 재조회해 갱신합니다.
- **다중 고객번호**: 한 계정에 고객번호가 2개 이상이면 통합 구성요소 추가 시 로그인 후 고객번호 선택 화면이 나옵니다(뒤 4자리로 표시). 선택한 번호마다 항목(기기)이 따로 생기며, 다른 번호는 같은 계정으로 항목을 한 번 더 추가하면 됩니다. 항목마다 별도 로그인 세션을 쓰고, 재로그인 때마다 고객번호를 다시 지정하므로 서로 덮어쓰지 않습니다. 이때 두 번째 이후 항목의 통계 ID는 `korea_incubator:kepco_<아이디>_<고객번호>_hourly_usage` / `..._hourly_export` 입니다(번호별로 분리). 고객번호가 1개인 계정은 이전과 동일하게 동작합니다.
- **잉여가 없는 고객번호**: 한전 응답의 발전설비 구분(`SESS_GENT_INST_CLCD`)이 비어 있고 조회 범위(최근 14일)에 잉여량>0 시간이 없으면 "최근 집계 시간 잉여량"/"오늘 잉여량 누계" 센서를 만들지 않고(기존 센서는 재시작/재로드 시 제거) `..._hourly_export` 통계도 더 이상 기록하지 않습니다(이미 쌓인 값과 대시보드 지정은 그대로 두며 갱신만 멈춥니다). 한 번이라도 잉여가 확인되면 항목에 저장되어 이후 잉여가 0인 날에도 센서가 유지되고, 처음 잉여가 생기면 항목이 자동 재로드되어 센서가 생깁니다.
- 한전 쪽에서 같은 아이디로 동시에 여러 세션이 유지되는지는 보장되지 않습니다. 세션이 끊기면 자동으로 재로그인합니다.

> Docker 설치/백업/복원 방법(위 "Docker 설치(HA Container)용")은 이 변경 후에도 그대로 유효합니다.

## 고객번호가 서로 섞였을 때(수정 내용과 복구)
- 증상: 두 번째 고객번호 항목이 첫 번째 번호의 사용량/잉여량/고객번호를 보여 줌. 한전 서버의 고객번호 선택 상태가 항목 간(같은 아이디)에 공유되거나 재로그인 때 기본 번호로 되돌아가, 로그인 때 한 번만 전환하던 이전 방식은 틀린 번호의 데이터를 읽을 수 있었습니다.
- 수정: 모든 응답의 `SESS_CUSTNO`가 항목의 고객번호와 같은지 매번 검증하고, 다르면 다시 전환해 재시도하며, 그래도 다르면 데이터를 버리고 갱신 실패(센서 사용 불가)로 처리합니다. 같은 아이디의 항목은 갱신을 직렬화하고, 번호를 저장하지 않은 기존 항목도 로그인 아이디와 같은 번호로 고정합니다.
- 통계 복구: 업데이트 후 재시작하면 첫 동기화에서 최근 14일을 다시 조회해 같은 시각의 `..._hourly_usage`/`..._hourly_export`를 올바른 값으로 덮어씁니다. 14일보다 오래전에 잘못 기록된 값이 있다면 개발자 도구 → 통계에서 해당 통계를 삭제한 뒤 재시작하세요(과거 값은 한전에서 다시 가져오지 않으므로 14일 이전은 복구되지 않습니다). 잘못 기록된 마지막 시간대가 올바른 데이터의 마지막 시간보다 뒤라면 그 시간대는 다음 한전 집계 시점에 덮어써집니다.

## 참고
- 한전은 아직 집계 전인 시간도 0.000으로 내려주므로, 그날 값이 있는 마지막 시간까지만 기록합니다.
- 한전 쪽 조회 실패는 기존 센서 갱신에 영향을 주지 않고 경고 로그만 남깁니다.

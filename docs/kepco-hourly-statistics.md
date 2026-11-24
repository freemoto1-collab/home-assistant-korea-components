# 한전(KEPCO) 시간별 사용량·잉여량 통계 — 설치/백업/복원 안내

## 무엇이 바뀌었나
- 한전 파워플래너 `POST /low/real/real_day/usage.do` (`{"SEARCH_DT": "YYYY-MM-DD"}`)로 시간별 수전 사용량(`F_AP_QT`)과 잉여량(역송, `F_AP_QT_REV`)을 조회합니다.
- 한전 표의 "N시"는 (N-1)시~N시 구간입니다. 통계는 그 구간의 **시작 시각**에 기록되어 에너지 대시보드 막대가 한전 표와 같은 시각에 찍힙니다.
- 새 통계(기존 센서/고유 ID는 그대로):
  - `korea_incubator:kepco_<고객번호>_hourly_usage` (kWh, 수전 사용량)
  - `korea_incubator:kepco_<고객번호>_hourly_export` (kWh, 잉여량)
- 최근 3일(재시작 직후 7일)을 30분 간격으로 다시 조회해 같은 시각을 덮어쓰므로 중복이 없고, 한전이 늦게 올린 시간대도 다음 조회에서 채워집니다.
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

## 참고
- 한전은 아직 집계 전인 시간도 0.000으로 내려주므로, 그날 값이 있는 마지막 시간까지만 기록합니다.
- 한전 쪽 조회 실패는 기존 센서 갱신에 영향을 주지 않고 경고 로그만 남깁니다.

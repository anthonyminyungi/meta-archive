# Meta Archive

Instagram 및 Facebook 계정 삭제 전 공식 JSON 반출본과 로컬 Chrome(CDP) 수집본을 결합해 무결성 검증 후 TrueNAS SCALE에 영구 보관·열람하기 위한 개인 아카이브 프로젝트입니다.

전체 아키텍처와 단계별 설계는 [implementation_plan.md](implementation_plan.md)를 참고하세요.

## 프로젝트 구조 (`pipeline/` ↔ `viewer/` 역할 분리)

- **`pipeline/` (`meta_archive_pipeline`) — Windows PC 전용 1회성 수집·검증·빌드 CLI**
  - 공식 ZIP 병합 해제, Mojibake 복구, Chrome CDP 수집, 무결성 검증, 다단계 매칭 병합, XMP 사이드카·`archive.sqlite`·`manifest.sha256` 생성을 담당합니다.
- **`viewer/` (`meta_archive_viewer`) — TrueNAS SCALE 배포 전용 읽기 전용 웹 뷰어 (Phase 4 구현 완료)**
  - 초경량 **Python FastAPI + Zero-Build ES Module JS/CSS** 기반 100% 오프라인 SPA(`Dockerfile` + TrueNAS Custom App `compose.yaml` 포함)로, `ARCHIVE_DIR`을 읽기 전용(`:ro`)으로 마운트해 열람·검색·HTTP `Range` 영상 스트리밍을 제공합니다.
- **`workspace_data/` *(Git 제외)*:** 로컬 작업 데이터(`chrome-profile/`, `official-raw/`, `collected/`, `archive/`) 저장 경로.

## 사전 요구 사항

- **데스크탑 (`pipeline/` 실행):**
  - [`uv`](https://docs.astral.sh/uv/) (Python 3.12+ 자동 관리)
  - 로컬 **Google Chrome** (`C:\Program Files\Google\Chrome\Application\chrome.exe`)
  - *참고:* 수집기는 실행 중인 로컬 Chrome에 CDP(`127.0.0.1:9222`)로 연결하므로 별도의 `playwright install`을 실행할 필요가 없습니다.
- **TrueNAS SCALE (`viewer/` 배포):**
  - Docker Compose 기반 Custom App YAML 빌드 지원

---

## `meta-archive` CLI 사용법 가이드 (Windows PowerShell 기준)

모든 CLI 명령은 저장소 루트(`c:\Users\dbstn\Documents\meta-archive`)에서 `uv --directory pipeline run meta-archive ...` 형태로 실행합니다.

### 0. 설치 및 동작 확인

```powershell
# pipeline 및 viewer 의존성 동기화
uv --directory pipeline sync --all-extras
uv --directory viewer sync --all-extras

# CLI 버전 및 서브커맨드 도움말 확인
uv --directory pipeline run meta-archive --help
uv --directory pipeline run meta-archive version

# 특정 명령 옵션 확인 (예: collect-ig)
uv --directory pipeline run meta-archive collect-ig --help

# 단위 테스트 실행
uv --directory pipeline run pytest -v
uv --directory viewer run pytest -v
```

### 1. 공식 Meta ZIP 안전 병합 해제 (`extract`)

Google Drive 등에서 받은 `...-1.zip`, `...-2.zip` 파일은 분할 압축이 아니라 같은 폴더 구조를 공유하는 독립 ZIP이므로, Windows 탐색기 대신 `extract` 명령(장경로 `\\?\` 및 해시 충돌 감지 지원)으로 한 폴더에 병합 해제합니다. (이미 수동 해제한 폴더가 있다면 이 단계는 건너뛰고 다음 단계에서 그 경로를 `--ig-official` 등에 바로 지정하면 됩니다.)

```powershell
uv --directory pipeline run meta-archive extract `
  --zip "C:\Users\dbstn\Downloads\instagram-export-1.zip" `
  --zip "C:\Users\dbstn\Downloads\instagram-export-2.zip" `
  --dest "workspace_data\official-raw\instagram"

uv --directory pipeline run meta-archive extract `
  --zip "C:\Users\dbstn\Downloads\facebook-export-1.zip" `
  --dest "workspace_data\official-raw\facebook"
```

### 2. 수집용 전용 Chrome 실행 & 수동 로그인 (`launch-chrome`)

계정 비밀번호는 CLI 인자나 설정 파일에 저장하지 않습니다. 전용 Chrome을 띄운 뒤 **사용자가 직접 로그인(2단계 인증 포함)** 합니다.

```powershell
uv --directory pipeline run meta-archive launch-chrome `
  --profile-dir "workspace_data\chrome-profile" `
  --port 9222 `
  --url "https://www.instagram.com/"
```

- 열린 브라우저 창에서 Instagram(필요 시 Facebook도 새 탭에서) 로그인을 완료합니다.
- **보안 주의:** 모든 웹 수집이 끝나면 세션 쿠키가 저장된 `workspace_data\chrome-profile` 폴더를 반드시 삭제하세요.

### 3. Instagram 웹 데이터 수집 (`collect-ig`)

공식 JSON에 빠진 타인 댓글·대댓글·좋아요 명단·하이라이트·프로필 아바타를 보강합니다. 처음에는 반드시 **`--limit 3` 또는 `--shortcodes`로 소량 스모크 테스트** 후 전체 실행을 권장합니다.

```powershell
# (권장) 1단계 스모크 테스트: 최근 3개 게시물만 수집 확인
uv --directory pipeline run meta-archive collect-ig `
  --username "본인_인스타_아이디" `
  --pass core `
  --limit 3 `
  --output-dir "workspace_data\collected\instagram"

# 특정 shortcode 몇 개만 지정 테스트할 때:
uv --directory pipeline run meta-archive collect-ig `
  --username "본인_인스타_아이디" `
  --pass core `
  --shortcodes "ABC1234,DEF5678"

# 2단계 전체 수집 (패스별 순차 실행 권장, 429/체크포인트 시 자동 일시중지)
uv --directory pipeline run meta-archive collect-ig --username "본인_인스타_아이디" --pass core
uv --directory pipeline run meta-archive collect-ig --username "본인_인스타_아이디" --pass likes
uv --directory pipeline run meta-archive collect-ig --username "본인_인스타_아이디" --pass connections
# (선택) 태그된 게시물 / 저장된 게시물
uv --directory pipeline run meta-archive collect-ig --username "본인_인스타_아이디" --pass tagged
uv --directory pipeline run meta-archive collect-ig --username "본인_인스타_아이디" --pass saved
```

- **중단 후 재개:** 수집 도중 멈추거나 429 쿨다운으로 일시 중지되어도 `--output-dir`의 `state.json` 체크포인트 덕분에 **같은 명령을 다시 실행하면 완료된 항목을 자동으로 건너뛰고 이어서 진행**합니다.
- `--no-download-avatars` 옵션을 주면 프로필 아바타 썸네일 다운로드를 생략할 수 있습니다.

### 4. Facebook 웹 데이터 수집 (`collect-fb`)

열린 Chrome에서 Facebook 로그인이 완료된 상태에서 패스별로 실행합니다.

```powershell
# 스모크 테스트 (게시물 3건)
uv --directory pipeline run meta-archive collect-fb --pass posts --limit 3

# 전체 수집 (타임라인 게시물+댓글, 친구 목록, 사진 앨범)
uv --directory pipeline run meta-archive collect-fb --pass posts
uv --directory pipeline run meta-archive collect-fb --pass friends
uv --directory pipeline run meta-archive collect-fb --pass albums
```

### 5. 무결성 검증 게이트 (`verify`)

공식 반출본과 웹 수집본의 URI↔파일 누락, 0바이트/손상 이미지·MP4, 무음 손상 영상(`ERROR`)과 연×월 공백·수량 차이(`WARN`)를 3중 교차 대조합니다. **`ERROR`가 1건이라도 있으면 종료 코드 `1`로 실패**하므로 계정 삭제 전 반드시 `PASS (ERROR=0)`를 확인하세요.

```powershell
uv --directory pipeline run meta-archive verify `
  --ig-official "C:\Users\dbstn\Downloads\instagram-yungis_-2026-11-20-9mAaRn8x" `
  --fb-official "C:\Users\dbstn\Downloads\facebook-dbstnsdl12-2026-11-22-EdI3t4uN" `
  --ig-collected "workspace_data\collected\instagram" `
  --fb-collected "workspace_data\collected\facebook" `
  --report-out "workspace_data\verify_report.md"
```

*(메신저 보안 저장소 암호화 대화가 있는 경우에만 `--fb-e2ee`를 추가합니다. 가진 입력 경로만 선택적으로 지정할 수 있으며, `.json` 확장자로 지정하면 JSON 리포트도 저장 가능합니다.)*

### 6. 최종 표준 아카이브 빌드 (`build`)

Mojibake 복구 → 다단계 `dHash`/캡션/시간 매칭 병합 → 원본 미디어 무손실 복사(위장 `.avif`→`.jpg` 및 무확장자 매직바이트 확장자 정규화 포함) + `.xmp` 타임존 사이드카 + `mtime` 동기화 → 내 댓글·저장 항목(`data/activities.json`) 및 읽기 전용 `archive.sqlite` (`FTS5 trigram` + `PRAGMA journal_mode=DELETE` + `VACUUM`) → `manifest.sha256` 생성 및 자체 검증까지 한 번에 수행합니다.

```powershell
uv --directory pipeline run meta-archive build `
  --ig-official "C:\Users\dbstn\Downloads\instagram-yungis_-2026-11-20-9mAaRn8x" `
  --fb-official "workspace_data\official-raw\facebook" `
  --fb-e2ee "workspace_data\official-raw\fb-e2ee" `
  --ig-collected "workspace_data\collected\instagram" `
  --fb-collected "workspace_data\collected\facebook" `
  --output-dir "workspace_data\archive" `
  --tz-offset "+09:00"
```

- 빌드가 끝나면 `workspace_data\archive\data\ambiguous_matches.json`에 보류된 모호 매칭 항목이 있는지 확인하세요.

### 7. 공식 재내보내기 없이 증분 점검 · 빈틈 복구 (`sync`)

공식 반출본을 다시 기다리지 않고, 기존 `workspace_data\archive` 기준으로 **이후에 추가된 IG DM / FB 메신저(E2EE 포함) / 신규 게시물·댓글·스토리·팔로우 변동**과 **기존 빈틈(FB 중복 빈 스텁 100건, IG DM 누락 이미지 13건, IG 무음 영상 10건 오디오 확인/복구)**을 Playwright(CDP)로 점검·반영할 수 있습니다.

```powershell
# 1단계: 전용 Chrome 실행 후 Instagram & Facebook 로그인 확인
uv --directory pipeline run meta-archive launch-chrome --profile-dir "workspace_data\chrome-profile" --port 9222

# 2단계: 사전 점검 (--dry-run, 아카이브를 건드리지 않고 추가/복구될 내역만 리포트)
uv --directory pipeline run meta-archive sync `
  --archive-dir "workspace_data\archive" `
  --cdp-url "http://127.0.0.1:9222" `
  --dry-run

# 3단계: 리포트 확인 후 실제 반영 (--apply: 자동 백업 → 병합 → 미디어/XMP → SQLite/manifest.sha256 재빌드)
uv --directory pipeline run meta-archive sync `
  --archive-dir "workspace_data\archive" `
  --cdp-url "http://127.0.0.1:9222" `
  --apply
```

- FB 중복 빈 스텁 정리만 브라우저 없이 바로 돌릴 때는 `--offline-only` 옵션을 붙이면 됩니다.
- FB 메신저에서 E2EE PIN 입력 화면이 뜨면 열린 Chrome 창에서 PIN을 입력할 때까지 기본 90초(`--wait-pin-sec`) 대기합니다.

---

## 로컬 웹 뷰어 미리보기 (`viewer/`)

TrueNAS로 옮기기 전에 Windows PC에서 완성된 `workspace_data\archive`를 바로 열어볼 수 있습니다.

```powershell
$env:ARCHIVE_DIR="c:\Users\dbstn\Documents\meta-archive\workspace_data\archive"
uv --directory viewer run uvicorn meta_archive_viewer.app:app --host 127.0.0.1 --port 8080
```

브라우저에서 `http://127.0.0.1:8080` 으로 접속해 Instagram/Facebook 피드·스토리·DM·한국어 검색(1~2글자 및 3글자+)·영상 시크바를 확인합니다.

---

## TrueNAS SCALE 전송 및 배포 흐름

1. **데이터·앱 1회 복사:** Windows에서 TrueNAS `meta-archive` 데이터셋(`/mnt/<pool>/meta-archive/`)으로 **SMB 공유 + `robocopy`(권장)** 또는 **SSH `scp`**를 사용해 아래 3개 폴더를 복사합니다.
   ```powershell
   # SMB 공유(예: \\truenas\meta-archive)가 연결된 경우 robocopy 예시
   robocopy "workspace_data\official-raw" "\\truenas\meta-archive\raw-cold" /E /COPY:DAT /R:3 /W:5
   robocopy "workspace_data\archive"      "\\truenas\meta-archive\archive"  /E /COPY:DAT /R:3 /W:5
   robocopy "viewer"                    "\\truenas\meta-archive\viewer"   /E /XD .venv __pycache__ .pytest_cache /R:3 /W:5
   ```
2. **체크섬 검증 & ZFS 스냅샷 (TrueNAS 셸):**
   ```bash
   cd /mnt/<pool>/meta-archive/archive
   sha256sum -c manifest.sha256 | grep -v ': OK$' || echo "ALL VERIFIED"
   ```
   모든 파일 검증이 끝나면 TrueNAS Web UI 또는 CLI에서 `meta-archive` 데이터셋의 **ZFS 스냅샷**을 생성합니다.
3. **Custom App YAML 배포:**
   - [viewer/compose.yaml](viewer/compose.yaml)에서 `<pool>` 두 곳을 실제 풀 이름(예: `tank`)으로 바꿉니다 (`build.context`는 반드시 `/mnt/<pool>/meta-archive/viewer` 절대 호스트 경로 유지, 아카이브는 `/mnt/<pool>/meta-archive/archive:/data/archive:ro` 마운트).
   - TrueNAS SCALE Apps → Discover Apps → Custom App(YAML)에 붙여 넣어 빌드·기동합니다.
4. **네트워크 보안 & 마무리 정리:**
   - 웹 뷰어는 앱 내 별도 로그인 없이 **LAN / Tailscale 내부망 전용**으로만 노출합니다.
   - 오프사이트 백업까지 완료한 뒤 로컬 `workspace_data\chrome-profile`을 삭제하고 SNS 계정 삭제를 진행합니다.

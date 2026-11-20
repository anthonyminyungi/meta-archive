# Meta Archive

Instagram 및 Facebook 계정 삭제 전 공식 JSON 반출본과 로컬 Chrome(CDP) 수집본을 결합해 무결성 검증 후 TrueNAS SCALE에 영구 보관·열람하기 위한 개인 아카이브 프로젝트입니다.

전체 아키텍처와 단계별 설계는 [implementation_plan.md](implementation_plan.md)를 참고하세요.

## 프로젝트 구조 (`pipeline/` ↔ `viewer/` 역할 분리)

- **`pipeline/` (`meta_archive_pipeline`) — Windows PC 전용 1회성 수집·검증·빌드 CLI**
  - 공식 ZIP 병합 해제, Mojibake 복구, Chrome CDP 수집, 무결성 검증, 다단계 매칭 병합, XMP 사이드카·`archive.sqlite`·`manifest.sha256` 생성을 담당합니다.
- **`viewer/` *(Phase 4 착수 시 생성 예정)* — TrueNAS SCALE 배포 전용 읽기 전용 웹 뷰어**
  - `pipeline/`으로 `archive/`를 완성한 뒤 Phase 4에서 구현합니다(기본 권장안: Python FastAPI + Zero-Build JS, 또는 Go / Vite+React 선택 가능).
- **`workspace_data/` *(Git 제외)*:** 로컬 작업 데이터(`chrome-profile/`, `official-raw/`, `collected/`, `archive/`) 저장 경로.

## 사전 요구 사항

- **데스크탑 (`pipeline/` 실행):**
  - [`uv`](https://docs.astral.sh/uv/) (Python 3.12+ 자동 관리)
  - 로컬 **Google Chrome** (`C:\Program Files\Google\Chrome\Application\chrome.exe`)
  - *참고:* 수집기는 실행 중인 로컬 Chrome에 CDP(`127.0.0.1:9222`)로 연결하므로 별도의 `playwright install`을 실행할 필요가 없습니다.
- **TrueNAS SCALE (`viewer/` 배포):**
  - Docker Compose 기반 Custom App YAML 빌드 지원

## 설치 및 실행 (`pipeline/`)

```powershell
# 의존성 동기화
uv --directory pipeline sync --all-extras

# CLI 도움말 및 버전 확인
uv --directory pipeline run meta-archive --help
uv --directory pipeline run meta-archive version

# 테스트 실행
uv --directory pipeline run pytest -v
```

## TrueNAS SCALE 전송 및 배포 흐름

1. **데이터·앱 1회 복사:** Windows에서 TrueNAS `meta-archive` 데이터셋(`/mnt/<pool>/meta-archive/`)으로 **SMB 공유 + `robocopy`(권장)** 또는 **SSH `scp`**를 사용해 아래 3개 폴더를 복사합니다.
   - `workspace_data/official-raw/` → `/mnt/<pool>/meta-archive/raw-cold/`
   - `workspace_data/archive/` → `/mnt/<pool>/meta-archive/archive/`
   - `viewer/` *(Phase 4 구현 후)* → `/mnt/<pool>/meta-archive/viewer/`
2. **체크섬 검증 & ZFS 스냅샷:** TrueNAS 셸에서 `sha256sum -c manifest.sha256`으로 `archive/` 무결성(0 failed)을 확인한 뒤 `meta-archive` 데이터셋의 ZFS 스냅샷을 생성합니다.
3. **Custom App YAML 배포:** `viewer/compose.yaml`의 `build.context`를 절대 호스트 경로(`/mnt/<pool>/meta-archive/viewer`)로 지정하고, `/mnt/<pool>/meta-archive/archive:/data/archive:ro` 읽기 전용 마운트로 배포합니다.
4. **네트워크 보안:** 웹 뷰어는 앱 내 별도 로그인 없이 **LAN / Tailscale 내부망 전용**으로 운영합니다.

# 모바일 앱 출시 문서

surge 읽기 전용 뷰어를 iOS/Android 스토어에 **공개·무료**로 올리기 위한 문서 모음입니다.
전체 로드맵(단계·규제·심사·타임라인)은 별도 계획서(아티팩트)로 관리합니다.

## 이 폴더

| 문서 | 용도 |
|---|---|
| [`PRIVACY_POLICY.md`](PRIVACY_POLICY.md) | 개인정보처리방침 초안 — **수집 0** 기준. 스토어 필수 URL 콘텐츠. |
| [`DISCLAIMER.md`](DISCLAIMER.md) | 면책 고지 전문 — 앱 최초 실행 게이트/약관/스토어 설명에 사용. |
| [`STORE_LISTING.md`](STORE_LISTING.md) | 스토어 등재 문구 초안(KR) — 이름·설명·키워드·카테고리·데이터안전. |

## 구현된 코드(이미 반영됨)

- PWA 레이어: `src/surge/dashboard/pwa.py` (manifest·서비스워커·아이콘)
- 아이콘 원본/생성기: `src/surge/dashboard/static/pwa/`, `tools/gen_pwa_icons.py`
- 면책 게이트 + PWA 메타: `src/surge/dashboard/export.py`
- Capacitor 래퍼 스캐폴드: [`../../mobile/`](../../mobile/)

## 남은 외부 작업(코드 아님)

- [ ] 유사투자자문 해당 여부 **법률 검토**
- [ ] Apple 조직 계정 + **D-U-N-S** 발급
- [ ] Google Play 계정 + **테스터 20인 × 14일** 폐쇄테스트
- [ ] **"surge" 상표** 충돌 확인, `appId` 확정
- [ ] 개인정보처리방침/지원 **URL 호스팅**
- [ ] 기기별 **스크린샷**(실기기 필요) 및 제출

*모든 초안은 법률 자문이 아니며, 제출 전 전문가 검토를 권장합니다.*

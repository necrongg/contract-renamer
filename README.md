# 근로계약서 파일명 변경기

PDF/이미지 근로계약서의 팀장명과 사용자명을 OCR로 읽어 파일명을 일괄 변경하는 Windows GUI입니다.

## 배포 파일

`release/근로계약서_파일명변경_배포용_180일인증.zip`을 내려받아 압축 해제 후 실행합니다. 실행 PC에는 Tesseract와 한국어 `kor` 데이터가 필요합니다. 최초 실행 시 PC 인증 ID가 표시되며, 관리자에게 발급받은 180일 코드를 입력해야 합니다.

## 소스 빌드

```powershell
py -m pip install -r requirements.txt
py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean --onedir --contents-directory _internal --windowed --icon contract_renamer.ico --add-data "contract_renamer.ico;." --name 근로계약서_파일명변경_배포용 contract_renamer.py
```

## 연락 및 권리

© 2026 인구강 · 무단 복제·수정·배포 금지 · necron7577@daum.net

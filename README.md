# 지금이닷 데이터 파이프라인

미국주식 매매일지 앱 '지금이닷'(가칭)의 데이터 수집 코드와 수집 결과.

- `pipeline/iex_daily/` IEX 거래소 공개 데이터에서 종목별 일별 종가 추출
- `data/iex/` 수집 결과 (`iex_YYYYMMDD.csv`, IEX 단일 거래소 체결 기준)
- `pipeline/ticker_names/` 미국 상장 종목 한글명·별칭 생성

## 데이터 출처

Data provided for free by [IEX](https://iextrading.com/trading/market-data/).
By accessing or using IEX Historical Data, you agree to the
[IEX Historical Data Terms of Use](https://www.iex.io/legal/hist-data-terms).

IEX 한 거래소의 체결만 담고 있어 미국 통합 공식 종가와 다를 수 있으며, 매매 판단의 근거가 아닌 참고용입니다.

"""
일일 데이터 수집 메인
매일 장 마감 후(16:00 ET) 자동 실행
1. 당일 상승률 1~10위 종목 추출 (시작가 $0.5~$50)
2. 프리마켓(04:00~09:30) + 본장(09:30~16:00) 1분봉 수집
3. 뉴스 수집
4. S3에 온톨로지 구조로 저장
5. 텔레그램으로 수집 완료 보고
"""

import os
import logging
import time
from datetime import datetime, date, timedelta
from typing import Optional

from dotenv import load_dotenv

from collector.polygon_client import PolygonClient
from collector.s3_writer import S3Writer
from reporter.telegram_reporter import TelegramReporter

load_dotenv()
logger = logging.getLogger(__name__)


class DailyCollector:
    def __init__(
        self,
        polygon_client: Optional[PolygonClient] = None,
        s3_writer: Optional[S3Writer] = None,
        telegram: Optional[TelegramReporter] = None,
    ):
        self.polygon = polygon_client or PolygonClient()
        self.s3 = s3_writer or S3Writer()
        self.telegram = telegram or TelegramReporter()

        self.min_price = float(os.environ.get("MIN_PRICE", "0.5"))
        self.max_price = float(os.environ.get("MAX_PRICE", "50.0"))
        self.min_volume = int(os.environ.get("MIN_VOLUME", "500000"))
        self.top_n = int(os.environ.get("TOP_N_TICKERS", "10"))

    def collect(self, trade_date: Optional[str] = None) -> dict:
        """
        특정 날짜의 데이터 수집
        trade_date: 'YYYY-MM-DD' (None이면 어제)
        """
        if trade_date is None:
            # 오늘이 월요일이면 금요일, 아니면 어제
            today = date.today()
            if today.weekday() == 0:  # 월요일
                trade_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            else:
                trade_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        logger.info(f"=== {trade_date} 데이터 수집 시작 ===")
        start_time = time.time()
        results = {
            "trade_date": trade_date,
            "tickers_collected": [],
            "errors": [],
            "total_rows": 0,
        }

        # 1. 상위 상승 종목 추출
        logger.info("상위 상승 종목 추출 중...")
        try:
            gainers = self.polygon.get_top_gainers(
                trade_date,
                min_price=self.min_price,
                max_price=self.max_price,
                min_volume=self.min_volume,
                top_n=self.top_n,
            )
        except Exception as e:
            logger.error(f"상위 종목 추출 실패: {e}")
            self.telegram.send_error(f"데이터 수집 실패 ({trade_date}): {e}")
            return results

        if not gainers:
            logger.warning(f"{trade_date}: 수집할 종목 없음")
            return results

        logger.info(f"수집 대상 종목 {len(gainers)}개: {[g['ticker'] for g in gainers]}")

        # 메타데이터 저장
        metadata = {
            "trade_date": trade_date,
            "collected_at": datetime.utcnow().isoformat(),
            "gainers": gainers,
            "total_tickers": len(gainers),
        }

        # 2. 각 종목별 데이터 수집
        for i, gainer in enumerate(gainers):
            ticker = gainer["ticker"]
            logger.info(f"[{i+1}/{len(gainers)}] {ticker} 수집 중... (상승률: {gainer.get('change_pct', 0):.1f}%)")

            try:
                ticker_result = self._collect_ticker(ticker, trade_date, gainer)
                results["tickers_collected"].append(ticker_result)
                results["total_rows"] += ticker_result.get("rows", 0)
            except Exception as e:
                logger.error(f"{ticker} 수집 실패: {e}")
                results["errors"].append({"ticker": ticker, "error": str(e)})

            time.sleep(1)  # API 레이트 리밋 방지

        # 메타데이터 저장
        metadata["collection_results"] = results
        self.s3.write_metadata(metadata, trade_date)

        elapsed = time.time() - start_time
        results["elapsed_seconds"] = elapsed

        # 3. 텔레그램 보고
        self._report_collection(trade_date, results, gainers)

        logger.info(f"=== 수집 완료: {len(results['tickers_collected'])}개 종목, {results['total_rows']}행, {elapsed:.1f}초 ===")
        return results

    def _collect_ticker(self, ticker: str, trade_date: str, gainer_info: dict) -> dict:
        """단일 종목 데이터 수집"""
        result = {"ticker": ticker, "rows": 0, "files": []}

        # 1분봉 (전체 세션: 04:00~16:00)
        df_bars = self.polygon.get_all_session_bars(ticker, trade_date)
        if not df_bars.empty:
            path = self.s3.write_dataframe(df_bars, trade_date, ticker, "minute_bars")
            result["files"].append(path)
            result["rows"] += len(df_bars)
            logger.info(f"  {ticker}: {len(df_bars)}개 1분봉 저장")

            # 프리마켓/본장 분리 저장
            premarket = df_bars[df_bars["session"] == "premarket"]
            regular = df_bars[df_bars["session"] == "regular"]

            if not premarket.empty:
                self.s3.write_dataframe(premarket, trade_date, ticker, "premarket_bars")
            if not regular.empty:
                self.s3.write_dataframe(regular, trade_date, ticker, "regular_bars")
        else:
            logger.warning(f"  {ticker}: 1분봉 데이터 없음")

        # 뉴스 수집
        news = self.polygon.get_news(ticker, trade_date, trade_date, limit=20)
        if news:
            self.s3.write_json(news, trade_date, ticker, "news")
            logger.info(f"  {ticker}: {len(news)}개 뉴스 저장")

        # 종목 기본 정보
        try:
            details = self.polygon.get_ticker_details(ticker)
            if details:
                self.s3.write_json(details, trade_date, ticker, "ticker_details")
        except Exception as e:
            logger.warning(f"  {ticker} 기본 정보 수집 실패: {e}")

        # gainer 정보 저장
        self.s3.write_json(gainer_info, trade_date, ticker, "gainer_info")

        return result

    def _report_collection(self, trade_date: str, results: dict, gainers: list) -> None:
        """텔레그램으로 수집 결과 보고"""
        success_count = len(results["tickers_collected"])
        error_count = len(results["errors"])
        total_rows = results["total_rows"]
        elapsed = results.get("elapsed_seconds", 0)

        # 상위 5개 종목 요약
        gainer_lines = []
        for g in gainers[:5]:
            ticker = g["ticker"]
            change = g.get("change_pct", 0)
            volume = g.get("volume", 0)
            gainer_lines.append(f"  {ticker}: +{change:.1f}% (거래량 {volume:,.0f})")

        message = (
            f"📊 *{trade_date} 데이터 수집 완료*\n\n"
            f"✅ 수집 성공: {success_count}개 종목\n"
            f"❌ 수집 실패: {error_count}개\n"
            f"📈 총 데이터: {total_rows:,}행\n"
            f"⏱️ 소요시간: {elapsed:.0f}초\n\n"
            f"🏆 *상위 종목:*\n" + "\n".join(gainer_lines)
        )

        if results["errors"]:
            error_tickers = [e["ticker"] for e in results["errors"]]
            message += f"\n\n⚠️ 실패 종목: {', '.join(error_tickers)}"

        self.telegram.send_message(message)

    def collect_range(self, start_date: str, end_date: str) -> list[dict]:
        """날짜 범위 수집 (주말 제외)"""
        from datetime import datetime
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        results = []
        current = start
        while current <= end:
            if current.weekday() < 5:  # 월~금
                result = self.collect(current.strftime("%Y-%m-%d"))
                results.append(result)
                time.sleep(2)  # 날짜 간 딜레이
            current += timedelta(days=1)

        return results


def run_collector(trade_date: Optional[str] = None):
    """수집 실행 진입점"""
    from utils.logger import setup_logger
    setup_logger()
    collector = DailyCollector()
    return collector.collect(trade_date)


if __name__ == "__main__":
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run_collector(date_arg)

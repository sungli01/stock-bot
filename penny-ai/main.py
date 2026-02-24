"""
penny-ai 메인 진입점

사용법:
  python main.py --mode collect    # 데이터 수집
  python main.py --mode process    # 피처 엔지니어링 + 케이스 분류
  python main.py --mode train      # AI 학습
  python main.py --mode simulate   # 백테스트
  python main.py --mode trade      # 실시간 매매
  python main.py --mode all        # 전체 파이프라인 (collect→process→train→simulate)
"""

import os
import sys
import logging
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("penny_ai.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("penny-ai")


def mode_collect(args):
    """일일 데이터 수집"""
    from collector.daily_collector import DailyCollector
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    logger.info(f"📊 데이터 수집 시작: {date}")
    collector = DailyCollector()
    result = collector.collect(date)
    logger.info(f"✅ 수집 완료: {result}")


def mode_process(args):
    """피처 엔지니어링 + 케이스 분류"""
    from processor.feature_engine import FeatureEngine
    from processor.event_detector import EventDetector
    from processor.case_classifier import CaseClassifier
    from utils.data_fabric import MarketDataFabric

    fabric = MarketDataFabric()
    feature_engine = FeatureEngine()
    event_detector = EventDetector()
    case_classifier = CaseClassifier()

    dates = fabric.list_dates()
    logger.info(f"🔧 피처 엔지니어링 시작: {len(dates)}일")

    for date in dates:
        tickers = fabric.list_tickers(date)
        for ticker in tickers:
            try:
                bars = fabric.get_timeseries(ticker, date)
                if bars is None or len(bars) < 20:
                    continue
                features = feature_engine.compute(bars)
                events = event_detector.detect(features)
                case = case_classifier.classify(events, features)
                logger.info(f"  {date} {ticker}: {case.get('type', '?')}형")
            except Exception as e:
                logger.error(f"  {date} {ticker} 오류: {e}")


def mode_train(args):
    """AI 학습"""
    from ai.trainer import Trainer
    logger.info("🧠 AI 학습 시작")
    trainer = Trainer()
    trainer.run()


def mode_simulate(args):
    """백테스트 시뮬레이션"""
    from simulation.backtester import Backtester
    from utils.data_fabric import MarketDataFabric
    from reporter.telegram_reporter import TelegramReporter

    logger.info("📈 백테스트 시작")
    fabric = MarketDataFabric()
    backtester = Backtester(
        initial_balance=float(os.environ.get("SEED_AMOUNT", 1_000_000))
    )
    reporter = TelegramReporter()

    # 데이터 로드
    data = {}
    dates = fabric.list_dates()
    for date in dates:
        tickers = fabric.list_tickers(date)
        day_data = []
        for ticker in tickers:
            bars = fabric.get_timeseries(ticker, date)
            case = fabric.get_case(ticker, date)
            events = fabric.get_events(ticker, date)
            if bars is not None:
                day_data.append({
                    "ticker": ticker,
                    "bars_df": bars,
                    "case": case or {},
                    "events": events or {}
                })
        if day_data:
            data[date] = day_data

    result = backtester.run(data)

    # 결과 보고
    summary = (
        f"📊 백테스트 완료!\n"
        f"기간: {result.get('period', 'N/A')}\n"
        f"초기 자본: {result.get('initial_balance', 0):,.0f}원\n"
        f"최종 자본: {result.get('final_balance', 0):,.0f}원\n"
        f"총 수익률: {result.get('total_return_pct', 0):+.2f}%\n"
        f"총 거래: {result.get('total_trades', 0)}건\n"
        f"승률: {result.get('win_rate', 0):.1f}%\n"
        f"MDD: {result.get('mdd', 0):.2f}%\n"
        f"샤프비율: {result.get('sharpe_ratio', 0):.2f}"
    )
    logger.info(summary)
    reporter.send(summary)


def mode_trade(args):
    """실시간 매매"""
    paper_mode = os.environ.get("PAPER_MODE", "true").lower() == "true"
    logger.info(f"🚀 트레이딩 엔진 시작 (PAPER_MODE={paper_mode})")

    from trading.engine import TradingEngine
    engine = TradingEngine(paper_mode=paper_mode)
    engine.run()


def mode_all(args):
    """전체 파이프라인"""
    logger.info("🔄 전체 파이프라인 시작")
    mode_collect(args)
    mode_process(args)
    mode_train(args)
    mode_simulate(args)


def main():
    parser = argparse.ArgumentParser(description="penny-ai 페니스탁 전용 AI 트레이딩 시스템")
    parser.add_argument(
        "--mode",
        choices=["collect", "process", "train", "simulate", "trade", "all"],
        default="trade",
        help="실행 모드"
    )
    parser.add_argument("--date", type=str, help="날짜 (YYYY-MM-DD, collect 모드용)")
    args = parser.parse_args()

    logger.info(f"{'='*50}")
    logger.info(f"🐾 penny-ai 시작 (mode={args.mode})")
    logger.info(f"{'='*50}")

    mode_map = {
        "collect": mode_collect,
        "process": mode_process,
        "train": mode_train,
        "simulate": mode_simulate,
        "trade": mode_trade,
        "all": mode_all,
    }

    mode_map[args.mode](args)


if __name__ == "__main__":
    main()

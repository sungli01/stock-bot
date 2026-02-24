"""
텔레그램 보고 모듈
- 매일 장 마감 후: 수집/학습 결과
- 매매 신호: 매수/매도 알림
- 일일 수익 리포트
- 주간/월간 성과 요약
"""

import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramReporter:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "5810895605")
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send(self, message: str) -> bool:
        """텔레그램 메시지 전송"""
        if not self.token:
            logger.warning("TELEGRAM_BOT_TOKEN 미설정 — 메시지 전송 생략")
            logger.info(f"[텔레그램 미전송] {message}")
            return False

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                },
                timeout=10
            )
            if resp.status_code == 200:
                logger.info(f"텔레그램 전송 완료: {message[:50]}...")
                return True
            else:
                logger.error(f"텔레그램 전송 실패: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"텔레그램 전송 오류: {e}")
            return False

    def report_collection(self, date: str, tickers: list, total_bars: int, errors: int):
        """데이터 수집 완료 보고"""
        msg = (
            f"📊 <b>일일 데이터 수집 완료</b>\n"
            f"날짜: {date}\n"
            f"수집 종목: {len(tickers)}개\n"
            f"총 1분봉: {total_bars:,}개\n"
            f"오류: {errors}건\n"
            f"종목: {', '.join(tickers[:5])}{'...' if len(tickers) > 5 else ''}"
        )
        return self.send(msg)

    def report_training(self, epoch: int, loss: float, val_accuracy: float, model_type: str):
        """AI 학습 결과 보고"""
        msg = (
            f"🧠 <b>AI 학습 완료</b>\n"
            f"모델: {model_type}\n"
            f"에포크: {epoch}\n"
            f"손실: {loss:.4f}\n"
            f"검증 정확도: {val_accuracy:.2%}"
        )
        return self.send(msg)

    def report_daily_pnl(self, date: str, trades: list, balance: float, initial_balance: float):
        """일일 손익 보고"""
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = len(trades) - wins
        win_rate = wins / len(trades) * 100 if trades else 0
        total_return = (balance - initial_balance) / initial_balance * 100

        emoji = "✅" if total_pnl >= 0 else "❌"
        msg = (
            f"{emoji} <b>일일 수익 리포트</b>\n"
            f"날짜: {date}\n"
            f"거래 수: {len(trades)}건\n"
            f"승/패: {wins}승 {losses}패 (승률 {win_rate:.1f}%)\n"
            f"일일 손익: {total_pnl:+,.0f}원\n"
            f"누적 수익률: {total_return:+.2f}%\n"
            f"잔고: {balance:,.0f}원"
        )
        return self.send(msg)

    def report_weekly_summary(self, week: str, stats: dict):
        """주간 성과 요약"""
        msg = (
            f"📈 <b>주간 성과 요약</b>\n"
            f"기간: {week}\n"
            f"총 거래: {stats.get('total_trades', 0)}건\n"
            f"승률: {stats.get('win_rate', 0):.1f}%\n"
            f"주간 수익률: {stats.get('weekly_return', 0):+.2f}%\n"
            f"MDD: {stats.get('mdd', 0):.2f}%\n"
            f"샤프비율: {stats.get('sharpe', 0):.2f}"
        )
        return self.send(msg)

    def report_buy_signal(self, ticker: str, price: float, qty: int, case_type: str,
                          amount: float, paper_mode: bool):
        """매수 신호 알림"""
        mode = "📄 PAPER" if paper_mode else "💰 실전"
        msg = (
            f"📈 <b>매수 신호 [{case_type}형]</b> {mode}\n"
            f"종목: <b>{ticker}</b>\n"
            f"가격: ${price:.4f}\n"
            f"수량: {qty:,}주\n"
            f"투자금: {amount:,.0f}원"
        )
        return self.send(msg)

    def report_sell_signal(self, ticker: str, price: float, pnl_pct: float,
                           pnl: float, reason: str, balance: float, paper_mode: bool):
        """매도 신호 알림"""
        mode = "📄 PAPER" if paper_mode else "💰 실전"
        emoji = "✅" if pnl > 0 else "❌"
        msg = (
            f"{emoji} <b>매도 [{reason}]</b> {mode}\n"
            f"종목: <b>{ticker}</b>\n"
            f"가격: ${price:.4f}\n"
            f"수익률: {pnl_pct:+.2f}%\n"
            f"손익: {pnl:+,.0f}원\n"
            f"잔고: {balance:,.0f}원"
        )
        return self.send(msg)

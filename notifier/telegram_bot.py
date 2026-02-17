"""
텔레그램 알림 모듈
- 종목 발굴 알림
- 매수/매도 완료 알림
- 손절 긴급 알림
- 일일 리포트
"""
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
USE_STUB = not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here"


class TelegramNotifier:
    """텔레그램 알림 봇"""

    def __init__(self):
        if not USE_STUB:
            from telegram import Bot
            self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
            self.chat_id = TELEGRAM_CHAT_ID
        else:
            self.bot = None
            self.chat_id = None
            logger.warning("⚠️ Telegram 토큰 없음 — stub 모드 (로그만 출력)")

    async def _send(self, text: str):
        """메시지 전송 (stub이면 로그만)"""
        if USE_STUB:
            logger.info(f"[TELEGRAM STUB]\n{text}")
            return
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")

    def send_sync(self, text: str):
        """동기 전송 (asyncio 없는 환경용)"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._send(text))
            else:
                loop.run_until_complete(self._send(text))
        except RuntimeError:
            asyncio.run(self._send(text))

    # ─── 알림 템플릿 ─────────────────────────────────────
    def notify_discovery(self, data: dict):
        """종목 발굴 알림"""
        text = (
            "🔍 종목 발굴\n"
            "━━━━━━━━━━━━━━\n"
            f"티커: {data.get('ticker', '?')}\n"
            f"현재가: ${data.get('price', 0):.2f}\n"
            f"5분 변동: {data.get('change_pct', 0):+.1f}%\n"
            f"거래량비: {data.get('volume_ratio', 0):.0f}%\n"
            f"시총: ${data.get('market_cap', 0):,.0f}\n"
            "━━━━━━━━━━━━━━\n"
            f"추세: {'📈 상승' if data.get('trend_direction') == 'UP' else '📉 하락' if data.get('trend_direction') == 'DOWN' else '➡️ 횡보'}"
            f" (신뢰도 {data.get('confidence', 0):.0f}%)"
        )
        self.send_sync(text)

    def notify_buy_complete(self, ticker: str, quantity: int, avg_price: float,
                            total_amount: float, take_profit: float, stop_loss: float):
        """매수 완료 알림"""
        text = (
            "✅ 매수 완료\n"
            "━━━━━━━━━━━━━━\n"
            f"티커: {ticker}\n"
            f"매수수량: {quantity}주 (10분할 완료)\n"
            f"평균매입가: ${avg_price:.2f}\n"
            f"총매수금액: ₩{total_amount:,.0f}\n"
            "━━━━━━━━━━━━━━\n"
            f"목표가(+30%): ${take_profit:.2f}\n"
            f"손절가(-15%): ${stop_loss:.2f}"
        )
        self.send_sync(text)

    def notify_sell(self, ticker: str, quantity: int, sell_price: float,
                    pnl_pct: float, pnl_amount: float, reason: str):
        """매도 알림"""
        emoji = "💰" if pnl_pct > 0 else "📉"
        text = (
            f"{emoji} 매도 실행\n"
            "━━━━━━━━━━━━━━\n"
            f"티커: {ticker}\n"
            f"매도수량: {quantity}주 (일괄)\n"
            f"매도가: ${sell_price:.2f}\n"
            f"수익률: {pnl_pct:+.1f}%\n"
            f"실현손익: ₩{pnl_amount:+,.0f}\n"
            "━━━━━━━━━━━━━━\n"
            f"사유: {reason}"
        )
        self.send_sync(text)

    def notify_stop_loss(self, ticker: str, quantity: int, price: float, pnl_pct: float):
        """손절 긴급 알림"""
        text = (
            "🚨 긴급 손절\n"
            "━━━━━━━━━━━━━━\n"
            f"티커: {ticker}\n"
            f"수량: {quantity}주\n"
            f"손절가: ${price:.2f}\n"
            f"손실률: {pnl_pct:.1f}%\n"
            "━━━━━━━━━━━━━━\n"
            "⚠️ 자동 손절 실행됨"
        )
        self.send_sync(text)

    def notify_daily_report(self, date: str, total_trades: int, total_pnl: float,
                            win_rate: float, details: Optional[dict] = None):
        """일일 리포트"""
        text = (
            f"📊 일일 리포트 ({date})\n"
            "━━━━━━━━━━━━━━\n"
            f"총 매매: {total_trades}건\n"
            f"총 손익: ₩{total_pnl:+,.0f}\n"
            f"승률: {win_rate:.1f}%\n"
            "━━━━━━━━━━━━━━"
        )
        if details:
            for ticker, d in details.items():
                text += f"\n  {ticker}: {d.get('pnl_pct', 0):+.1f}%"
        self.send_sync(text)

"""Batch runner for sim7: 2021-11-04 to 2022-05-31"""
import subprocess
import sys
import time
import os
from datetime import datetime, timedelta

def get_remaining_days():
    start = datetime(2021, 11, 1)
    end = datetime(2022, 5, 31)
    d = start
    all_days = []
    while d <= end:
        if d.weekday() < 5:
            all_days.append(d.strftime('%Y-%m-%d'))
        d += timedelta(days=1)
    
    # Check which already exist
    done = set()
    bt_dir = "data/backtest"
    if os.path.exists(bt_dir):
        for f in os.listdir(bt_dir):
            if f.endswith('.json') and (f.startswith('2021-') or f.startswith('2022-0')):
                done.add(f.replace('.json', ''))
    
    remaining = [d for d in all_days if d not in done]
    print(f"Total: {len(all_days)}, Done: {len(done)}, Remaining: {len(remaining)}")
    return remaining

def run_day(date, attempt=0):
    try:
        result = subprocess.run(
            [sys.executable, "backtest.py", date],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            print(f"  ❌ {date} failed: {result.stderr[-200:]}")
            if attempt < 2:
                print(f"  ⏳ Retry in 30s...")
                time.sleep(30)
                return run_day(date, attempt + 1)
            return False
        # Check if output mentions error
        if "종목 없음" in result.stdout:
            print(f"  ⚠️ {date}: 휴장일/데이터없음")
        else:
            # Extract PnL from output
            for line in result.stdout.split('\n'):
                if '총 손익' in line:
                    print(f"  ✅ {date}: {line.strip()}")
                    break
            else:
                print(f"  ✅ {date}: done")
        return True
    except subprocess.TimeoutExpired:
        print(f"  ⏰ {date} timeout")
        if attempt < 2:
            time.sleep(30)
            return run_day(date, attempt + 1)
        return False
    except Exception as e:
        print(f"  ❌ {date} error: {e}")
        return False

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    days = get_remaining_days()
    
    for i, date in enumerate(days):
        print(f"[{i+1}/{len(days)}] {date}")
        run_day(date)
        # Rate limit: wait between days
        if (i + 1) % 10 == 0:
            print(f"  💤 Batch pause (10 days done)... 15s")
            time.sleep(15)
        else:
            time.sleep(2)
    
    print("\n✅ All done!")

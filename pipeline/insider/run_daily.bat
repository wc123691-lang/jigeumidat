@echo off
rem Jigeumidat: collect SEC Form 4 insider purchases/sales, build per-ticker history, push to GitHub.
rem Run daily by Windows Task Scheduler. SEC_USER_AGENT comes from the user environment
rem (setx SEC_USER_AGENT "Jigeumidat your@email") so the email is not stored in this public repo.
set PYTHONIOENCODING=utf-8
cd /d "%~dp0..\.."
rem --autostash: if an earlier run was cut off before its commit, keep its data instead of failing the pull
git pull -q --rebase --autostash
python pipeline\insider\form4.py >> pipeline\insider\run.log 2>&1
rem exit code 3: another collection is still running (output\form4.lock); it will build and push when done
if errorlevel 3 exit /b 0
rem recent 1-year closes first: history.py uses them to find listing dates (drops IPO-day purchases)
python pipeline\signals\export_prices.py >> pipeline\insider\run.log 2>&1
python pipeline\insider\history.py >> pipeline\insider\run.log 2>&1
rem stock splits confirmed with SEC share counts (SEC blocks GitHub Actions); the afternoon signal run reuses them
python pipeline\signals\price_history.py >> pipeline\insider\run.log 2>&1
rem House members' stock trade reports (PTR) and per-trade returns (needs full price history in output\history)
python pipeline\congress\house.py >> pipeline\insider\run.log 2>&1
python pipeline\signals\export_history.py >> pipeline\insider\run.log 2>&1
python pipeline\congress\house_returns.py >> pipeline\insider\run.log 2>&1
rem insider cluster-buy backtest shown in the app (also needs output\history)
python pipeline\insider\backtest.py >> pipeline\insider\run.log 2>&1
git add data\insider data\congress data\signals\split_decisions_full.csv
rem the run takes ~30 min and GitHub Actions push closes meanwhile: rebase onto them before pushing
git diff --cached --quiet || (git commit -q -m "Insider trades" && git pull -q --rebase && git push -q)

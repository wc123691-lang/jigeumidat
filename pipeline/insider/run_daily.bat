@echo off
rem Jigeumidat: collect SEC Form 4 insider purchases and push to GitHub.
rem Run daily by Windows Task Scheduler. SEC_USER_AGENT comes from the user environment
rem (setx SEC_USER_AGENT "Jigeumidat your@email") so the email is not stored in this public repo.
set PYTHONIOENCODING=utf-8
cd /d "%~dp0..\.."
git pull -q --rebase
python pipeline\insider\form4.py >> pipeline\insider\run.log 2>&1
git add data\insider
git diff --cached --quiet || (git commit -q -m "Insider purchases" && git push -q)

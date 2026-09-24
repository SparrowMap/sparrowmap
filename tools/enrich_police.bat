@echo off
rem Resumable party enrichment. Safe to re-run: the queue is parties_checked IS NULL.
rem
rem BOTH SCOPES, IN ONE PROCESS, ON PURPOSE. The CourtListener anonymous ceiling
rem is a ROLLING BUDGET of a few hundred requests, not a per-minute rate, so two
rem enrichers each pacing politely at --delay 240 spend it twice as fast as
rem either believes and buy a lockout for both. police first: it is the slice the
rem review surface needs to open. federal follows (FBI/DHS/ICE/DEA/USMS - they
rem are sued under Bivens, not 1983, see oversight.py).
cd /d D:\LLM\sparrow
echo === started %date% %time% --enrich police,federal --delay 240 --patient >> data\enrich_police.log
python -u tools\courtlistener_fetch.py --enrich police,federal --delay 240 --patient >> data\enrich_police.out.log 2>> data\enrich_police.err.log

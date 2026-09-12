@echo off
rem Resumable police-party enrichment. Safe to re-run: the queue is parties_checked IS NULL.
cd /d D:\LLM\sparrow
echo === started %date% %time% --delay 240 --patient (launcher) >> data\enrich_police.log
python -u tools\courtlistener_fetch.py --enrich police --delay 240 --patient >> data\enrich_police.out.log 2>> data\enrich_police.err.log

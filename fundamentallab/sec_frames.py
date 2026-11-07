from __future__ import annotations
from collections import defaultdict
from datetime import date
import time, requests, threading
from .database import upsert_company
from .models import CompanyContext

SEC_DATA='https://data.sec.gov'; SEC_TICKERS='https://www.sec.gov/files/company_tickers.json'
FRAME_METRICS={
 'revenue':('USD',['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet']),
 'net_income':('USD',['NetIncomeLoss','ProfitLoss']),
 'cfo':('USD',['NetCashProvidedByUsedInOperatingActivities']),
 'capex':('USD',['PaymentsToAcquirePropertyPlantAndEquipment','PaymentsForAdditionsToPropertyPlantAndEquipment']),
 'buybacks':('USD',['PaymentsForRepurchaseOfCommonStock']),
 'diluted_shares':('shares',['WeightedAverageNumberOfDilutedSharesOutstanding']),
}
class SecFramesClient:
    def __init__(self,email,min_interval=.12):
        if not email or '@' not in email: raise ValueError('SEC requiere email real de contacto.')
        self.s=requests.Session(); self.s.headers.update({'User-Agent':f'FundamentalLab/2.5 {email}','Accept':'application/json','Accept-Encoding':'gzip, deflate'})
        self.min_interval=min_interval; self.last=0.0; self._rate_lock=threading.Lock()
    def _get(self,url,timeout=30):
        # Rate limiter thread-safe. La SEC limita el acceso automatizado; este cliente
        # mantiene margen por debajo del umbral incluso durante el fallback concurrente.
        with self._rate_lock:
            e=time.monotonic()-self.last
            if e<self.min_interval: time.sleep(self.min_interval-e)
            self.last=time.monotonic()
        last_exc=None
        for attempt in range(2):
            try:
                r=self.s.get(url,timeout=timeout)
                if r.status_code==404:return None
                if r.status_code in (429,500,502,503,504) and attempt==0:
                    time.sleep(0.8); continue
                r.raise_for_status(); return r.json()
            except Exception as exc:
                last_exc=exc
                if attempt==0:
                    time.sleep(0.5); continue
        if last_exc: raise last_exc
        return None
    def tickers(self): return self._get(SEC_TICKERS)
    def frame(self,tag,unit,period,taxonomy='us-gaap'): return self._get(f'{SEC_DATA}/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json')
    def companyfacts(self,cik): return self._get(f'{SEC_DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json',timeout=25)

def completed_quarters(n=4,today=None):
    today=today or date.today(); cq=(today.month-1)//3+1; y=today.year; q=cq-1
    if q==0:y-=1;q=4
    out=[]
    for _ in range(n):
        out.append(f'CY{y}Q{q}'); q-=1
        if q==0:y-=1;q=4
    return list(reversed(out))

def primary_map(raw):
    out={}
    for x in raw.values():
        cik=int(x['cik_str']); ticker=str(x['ticker']).upper(); name=str(x['title'])
        if cik not in out: out[cik]={'ticker':ticker,'name':name,'aliases':[ticker]}
        else: out[cik]['aliases'].append(ticker)
    return out

def build_us_contexts(conn,client,periods=None):
    periods=periods or completed_quarters(4); companies=primary_map(client.tickers())
    for cik,x in companies.items(): upsert_company(conn,'USA',x['ticker'],x['name'],str(cik),x['aliases'],'SEC company_tickers.json')
    conn.commit(); vals={m:defaultdict(dict) for m in FRAME_METRICS}
    for metric,(unit,tags) in FRAME_METRICS.items():
        for p in periods:
            seen=set()
            for tag in tags:
                payload=client.frame(tag,unit,p)
                if not payload: continue
                for row in payload.get('data',[]):
                    try:cik=int(row['cik']);v=float(row['val'])
                    except:continue
                    if cik in seen:continue
                    vals[metric][cik][p]=v;seen.add(cik)
    # Fallback de share count para huecos de diluted shares.
    # Los facts instantáneos usan frame CYxxxxQnI. EntityCommonStockSharesOutstanding
    # (DEI) no es idéntico a diluted weighted shares, pero es una base razonable para
    # FCF/share cuando SEC no expone el tag diluido en ese quarter.
    outstanding = defaultdict(dict)
    for p in periods:
        instant_period = p + 'I'
        seen = set()
        for taxonomy, tag in [
            ('dei', 'EntityCommonStockSharesOutstanding'),
            ('us-gaap', 'CommonStockSharesOutstanding'),
        ]:
            payload = client.frame(tag, 'shares', instant_period, taxonomy=taxonomy)
            if not payload:
                continue
            for row in payload.get('data', []):
                try:
                    cik = int(row['cik']); v = float(row['val'])
                except Exception:
                    continue
                if cik in seen:
                    continue
                outstanding[cik][p] = v
                seen.add(cik)

    contexts=[]
    for cik,x in companies.items():
        ser=lambda m:[vals[m].get(cik,{}).get(p) for p in periods]
        rev,ni,cfo,cap,buy,shr=map(ser,['revenue','net_income','cfo','capex','buybacks','diluted_shares'])
        shr=[v if v is not None else outstanding.get(cik,{}).get(p) for v,p in zip(shr,periods)]
        fcf=[None if a is None or b is None else float(a)-abs(float(b)) for a,b in zip(cfo,cap)]
        if not any(v is not None for seq in [rev,ni,cfo,cap,buy,shr] for v in seq):continue
        contexts.append(CompanyContext('USA',x['ticker'],x['name'],periods,rev,ni,cfo,cap,fcf,buy,shr,
            metadata={
                'cik':cik,
                'aliases':x['aliases'],
                'source':'SEC XBRL Frames',
                'shares_outstanding': next(
                    (v for v in reversed(shr) if v is not None),
                    None
                ),
                'financial_currency':'USD',
                'market_currency':'USD',
                'dcf_currency_multiplier':1.0,
            }))
    return contexts

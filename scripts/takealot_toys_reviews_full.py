#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,random,threading,time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timedelta
from pathlib import Path
from curl_cffi import requests

API='https://api.takealot.com/rest/v-1-16-0'
REVIEW=f'{API}/product-reviews/plid/{{plid}}'
BASE=datetime(2026,8,25)
C30=BASE-timedelta(days=30);C90=BASE-timedelta(days=90);C180=BASE-timedelta(days=180);C365=BASE-timedelta(days=365)
CUTOFF=datetime(2025,1,1)
IM=('chrome131','chrome124','safari184')
_tls=threading.local()

def sess():
    s=getattr(_tls,'s',None)
    if s is None:
        s=requests.Session(impersonate=random.choice(IM),timeout=25)
        s.headers.update({'accept':'application/json','accept-language':'en-ZA,en;q=0.9','referer':'https://www.takealot.com/','user-agent':'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36'});_tls.s=s
    return s

def reset():
    try:delattr(_tls,'s')
    except Exception:pass

def get_json(url,params=None,tries=7):
    last=''
    for n in range(tries):
        try:
            r=sess().get(url,params=params,timeout=25)
            if r.status_code==200:return r.json()
            last=f'HTTP {r.status_code}: {r.text[:120]}'
        except Exception as e:last=str(e)
        reset();time.sleep(min(8,.35*(2**n))+random.random()*.25)
    raise RuntimeError(last or 'request failed')

def dt(v):
    if not v:return None
    t=str(v).strip()
    for f in ('%d %b %Y','%Y-%m-%d','%d %B %Y'):
        try:return datetime.strptime(t,f)
        except ValueError:pass
    return None

def rpage(plid,page,sort='SO_LATEST'):
    d=get_json(REVIEW.format(plid=plid),{'sort':sort,'page':page})
    return d.get('reviews') or [],int(((d.get('page_info') or {}).get('total')) or 0)

def inum(v):
    try:return int(float(str(v or '0').replace(',','')))
    except:return 0

def process(p,page_cap=500):
    total=inum(p.get('total_reviews'));base=dict(p)
    if total<=0:
        base.update({'api_total_reviews':0,'first_review_date':'','latest_review_date':'','days_since_latest':'','reviews_30d':0,'reviews_90d':0,'reviews_180d':0,'reviews_365d':0,'reviews_since_2025':0,'review_pages_scanned':0,'review_page_cap_hit':False,'capture_complete_since_2025':True,'review_error':''})
        return base,[]
    try:
        first_page,api_total=rpage(p['plid'],0,'SO_LATEST')
        if not first_page:
            base.update({'api_total_reviews':api_total or total,'first_review_date':'','latest_review_date':'','days_since_latest':'','reviews_30d':0,'reviews_90d':0,'reviews_180d':0,'reviews_365d':0,'reviews_since_2025':0,'review_pages_scanned':1,'review_page_cap_hit':False,'capture_complete_since_2025':False,'review_error':'no reviews returned'})
            return base,[]
        latest=None
        for r in first_page:
            x=dt(r.get('date'))
            if x and x<=BASE+timedelta(days=1):latest=x;break
        try:
            oldest_page,_=rpage(p['plid'],0,'SO_OLDEST');first=None
            for r in oldest_page:
                x=dt(r.get('date'))
                if x:first=x;break
        except Exception:first=None
        dates=[];raw=[];page=0;current=first_page;hit_old=False;cap=False;err=''
        while True:
            for r in current:
                x=dt(r.get('date'))
                if not x:continue
                if x<CUTOFF:hit_old=True;break
                if x<=BASE+timedelta(days=1):dates.append(x);raw.append((p['plid'],x.strftime('%Y-%m-%d'),r.get('rating') or ''))
            if hit_old or len(current)<10:break
            page+=1
            if page>=page_cap:cap=True;break
            try:current,_=rpage(p['plid'],page,'SO_LATEST')
            except Exception as e:err=str(e);break
            if not current:break
        monthly=Counter(x.strftime('%Y-%m') for x in dates);c30=sum(x>=C30 for x in dates);c90=sum(x>=C90 for x in dates);c180=sum(x>=C180 for x in dates);c365=sum(x>=C365 for x in dates)
        if latest is None and dates:latest=max(dates)
        out=base;out.update({'api_total_reviews':api_total or total,'first_review_date':first.strftime('%Y-%m-%d') if first else '','latest_review_date':latest.strftime('%Y-%m-%d') if latest else '','days_since_latest':(BASE-latest).days if latest else '','reviews_30d':c30,'reviews_90d':c90,'reviews_180d':c180,'reviews_365d':c365,'reviews_since_2025':len(dates),'review_pages_scanned':page+1,'review_page_cap_hit':cap,'capture_complete_since_2025':bool(hit_old or len(current)<10) and not cap and not err,'review_error':err})
        for y in (2025,2026):
            for m in range(1,13):out[f'reviews_{y}_{m:02d}']=monthly.get(f'{y}-{m:02d}',0)
        return out,raw
    except Exception as e:
        base.update({'api_total_reviews':total,'first_review_date':'','latest_review_date':'','days_since_latest':'','reviews_30d':'','reviews_90d':'','reviews_180d':'','reviews_365d':'','reviews_since_2025':'','review_pages_scanned':0,'review_page_cap_hit':False,'capture_complete_since_2025':False,'review_error':str(e)});return base,[]

BASE_FIELDS=['plid','url','slug','title','brand','price','star_rating','total_reviews','main_image_url','top_category','price_partition']
STAT_FIELDS=['api_total_reviews','first_review_date','latest_review_date','days_since_latest','reviews_30d','reviews_90d','reviews_180d','reviews_365d','reviews_since_2025']
MONTH_FIELDS=[f'reviews_{y}_{m:02d}' for y in (2025,2026) for m in range(1,13)]
TAIL=['review_pages_scanned','review_page_cap_hit','capture_complete_since_2025','review_error'];FIELDS=BASE_FIELDS+STAT_FIELDS+MONTH_FIELDS+TAIL

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--out',required=True);ap.add_argument('--workers',type=int,default=18);ap.add_argument('--page-cap',type=int,default=500);a=ap.parse_args();out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    with open(a.input,encoding='utf-8-sig',newline='') as f:products=list(csv.DictReader(f))
    summaries=[];review_rows=[];done=0;errors=0;t0=time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs={ex.submit(process,p,a.page_cap):p.get('plid') for p in products}
        for fut in as_completed(futs):
            try:s,rr=fut.result()
            except Exception as e:s={'plid':futs[fut],'review_error':str(e)};rr=[]
            summaries.append(s);review_rows.extend(rr);done+=1;errors+=bool(s.get('review_error'))
            if done%500==0:print(f'progress {done}/{len(products)} review_rows={len(review_rows)} errors={errors} elapsed={time.time()-t0:.1f}s',flush=True)
    summaries.sort(key=lambda x:inum(x.get('plid')))
    with (out/'review_summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore');w.writeheader();w.writerows(summaries)
    with (out/'review_dates_since_2025.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['plid','review_date','rating']);w.writerows(review_rows)
    stats={'input_skus':len(products),'summary_rows':len(summaries),'historic_review_skus':sum(inum(x.get('total_reviews'))>0 for x in summaries),'zero_review_skus':sum(inum(x.get('total_reviews'))<=0 for x in summaries),'review_date_rows_since_2025':len(review_rows),'errors':errors,'capture_complete_rows':sum(str(x.get('capture_complete_since_2025')).lower()=='true' for x in summaries),'page_cap_hits':sum(str(x.get('review_page_cap_hit')).lower()=='true' for x in summaries),'base_date':'2026-08-25','cutoff':'2025-01-01','elapsed_seconds':round(time.time()-t0,1)}
    (out/'progress.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(stats,ensure_ascii=False,indent=2),flush=True)
if __name__=='__main__':main()

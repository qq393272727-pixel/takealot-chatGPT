#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,random,time
from pathlib import Path
from curl_cffi import requests

API='https://api.takealot.com/rest/v-1-16-0'
SECTIONS='products,filters,facets,sort_options,breadcrumbs,slots_audience,context,seo,layout'
SEARCH=f'{API}/searches/{SECTIONS}'
TOPS={
0:('Indoor Play','indoor-play-25402',49201),
1:('Games & Puzzles','games-and-puzzles-25345',12036),
2:('Arts & Crafts','arts-and-crafts-25473',11550),
3:('Party Supplies','party-supplies-25480',7805),
4:('Outdoor Play','outdoor-play-25380',5697),
5:('Pretend Play','pretend-play-29202',4725),
6:('Smart & Interactive Toys','smart-and-interactive-toys-25363',2599),
7:('Fancy Dress','fancy-dress-25479',1857),
8:('LEGO','lego-25403',1646),
9:('Bikes, Scooters & Rides','bikes-scooters-and-rides-25396',1621),
10:('Kids Decor','kids-decor-25469',1076),
11:('Kids Watches','kids-watches-29358',157),
12:('Merchandise','merchandise-33648',110),
}
IM=('chrome131','chrome124','safari184')

def session():
    s=requests.Session(impersonate=random.choice(IM),timeout=25)
    s.headers.update({'accept':'application/json','accept-language':'en-ZA,en;q=0.9','referer':'https://www.takealot.com/','user-agent':'Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36'})
    return s

def get_json(s,params,tries=8):
    last=''
    for n in range(tries):
        try:
            r=s.get(SEARCH,params=params,timeout=25)
            if r.status_code==200:return r.json()
            last=f'HTTP {r.status_code}: {r.text[:100]}'
        except Exception as e:last=str(e)
        time.sleep(min(8,0.35*(2**n))+random.random()*.2)
    raise RuntimeError(last or 'request failed')

def parse_product(it,cat_name,price_filter):
    pv=it.get('product_views') or {}; core=pv.get('core') or {}; buy=pv.get('buybox_summary') or {}
    plid=core.get('id')
    if not plid:return None
    slug=core.get('slug') or 'product'
    prices=buy.get('prices') or []
    image=''
    for obj in (core,pv):
        if not isinstance(obj,dict):continue
        for k in ('image_url','image','thumbnail','thumbnail_url'):
            v=obj.get(k)
            if isinstance(v,str) and v.startswith('http'): image=v;break
        if image:break
    return {
        'plid':str(plid),'url':f'https://www.takealot.com/{slug}/PLID{plid}','slug':slug,
        'title':core.get('title') or '','brand':core.get('brand') or '',
        'price':prices[0] if prices else buy.get('listing_price',''),
        'star_rating':core.get('star_rating',''),'total_reviews':core.get('reviews',core.get('review_count','')),
        'main_image_url':image,'top_category':cat_name,'price_partition':price_filter or 'ALL'
    }

def page(s,slug,after=None,price_filter=None):
    p={'department_slug':'toys','category_slug':slug}
    if after:p['after']=after
    if price_filter:p['filter']=f'Price:{price_filter}'
    d=get_json(s,p); sec=d.get('sections') or {}; pr=sec.get('products') or {}; pg=pr.get('paging') or {}
    return pr.get('results') or [],pg.get('next_is_after'),int(pg.get('total_num_found') or 0),sec

def exact_category_count(sec):
    for item in ((sec.get('facets') or {}).get('results') or []):
        fac=item.get('facet') or {}; df=fac.get('discrete_facet') or {}
        if df.get('filter_name')=='Type':
            for e in df.get('entries') or []:
                if e.get('is_checked') and e.get('value')=='7':
                    try:return int(e.get('num_docs'))
                    except:return None
    return None

def enumerate_query(s,slug,cat_name,price_filter,first_items=None,first_next=None,first_total=None,max_pages=250):
    out={}; items=first_items; nxt=first_next; total=first_total; pages=0; after=None; seen_cursors=set()
    while True:
        if items is None: items,nxt,total,_=page(s,slug,after,price_filter)
        pages+=1
        for it in items:
            p=parse_product(it,cat_name,price_filter)
            if p:out[p['plid']]=p
        if not nxt or len(items)==0 or nxt in seen_cursors or pages>=max_pages:break
        seen_cursors.add(nxt); after=nxt; items=None
    return out,pages,total

def split_range(lo,hi):
    mid=(lo+hi)//2
    if mid<=lo:mid=lo+1
    return (lo,mid),(mid,hi)

def collect_partition(s,slug,cat_name,lo,hi,depth=0):
    filt=f'{lo}-{hi}';items,nxt,total,sec=page(s,slug,None,filt)
    if total<5000 or hi-lo<=1:
        data,pages,_=enumerate_query(s,slug,cat_name,filt,items,nxt,total)
        return data,[{'filter':filt,'reported_total':total,'rows':len(data),'pages':pages,'depth':depth,'capped':bool(total>=5000 and hi-lo<=1)}]
    a,b=split_range(lo,hi)
    d1,s1=collect_partition(s,slug,cat_name,a[0],a[1],depth+1);d2,s2=collect_partition(s,slug,cat_name,b[0],b[1],depth+1);d1.update(d2)
    return d1,s1+s2

def collect_category(idx,outdir):
    name,slug,front_count=TOPS[idx];s=session();items,nxt,total,sec=page(s,slug);exact=exact_category_count(sec)
    ranges=[(0,150),(150,500),(500,800),(800,1000),(1000,2000),(2000,3000),(3000,5000),(5000,8000),(8000,12000),(12000,25000),(25000,50000),(50000,1000000)]
    allp={};stats=[]
    if total<5000 and (exact is None or exact<5000):
        allp,pages,_=enumerate_query(s,slug,name,None,items,nxt,total);stats.append({'filter':'ALL','reported_total':total,'rows':len(allp),'pages':pages,'depth':0})
    else:
        for lo,hi in ranges:
            d,st=collect_partition(s,slug,name,lo,hi);allp.update(d);stats.extend(st);print(f'{name}: {lo}-{hi} cumulative={len(allp)} exact={exact}',flush=True)
    outdir.mkdir(parents=True,exist_ok=True)
    fields=['plid','url','slug','title','brand','price','star_rating','total_reviews','main_image_url','top_category','price_partition']
    with (outdir/'links.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(allp.values())
    progress={'index':idx,'top_category':name,'category_slug':slug,'screenshot_count_20260824':front_count,'api_exact_count_at_start':exact,'unique_links':len(allp),'partitions':len(stats),'partition_stats':stats,'coverage_vs_api_exact':(len(allp)/exact if exact else None)}
    (outdir/'progress.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(progress,ensure_ascii=False),flush=True)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--index',type=int,required=True);ap.add_argument('--out',required=True);a=ap.parse_args();collect_category(a.index,Path(a.out))
if __name__=='__main__':main()

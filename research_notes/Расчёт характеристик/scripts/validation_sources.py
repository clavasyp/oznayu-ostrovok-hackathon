"""Freeze an explicit list of primary validation pages; never visit OTA hosts.

Native curl uses the machine's certificate store. HTTPS verification stays on.
This is a source audit helper, not a general website crawler or dataset licence.
"""
import hashlib,json,subprocess,tempfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from common import BASE

URLS={
 'wmo_normals':'https://www.ncei.noaa.gov/products/wmo-climate-normals',
 'sotchi_station':'https://www.nodc.noaa.gov/archive/arc0216/0253808/4.4/data/0-data/Region-6-WMO-Normals-9120/RussianFederation/CSV/Sotchi_37099.csv',
 'dubai_airport_metro':'https://dubaiairports.ae/transport/metro',
 'dubai_rta_timetable':'https://www.rta.ae/wps/portal/rta/ae/public-transport/timetable?lang=en',
 'sochi_routes':'https://www.sochi-tk.ru/routes.html',
 'radisson_official':'https://www.radissonhotels.com/en-us/hotels/radisson-collection-resort-sochi-spa',
 'radisson_rooms':'https://www.radissonhotels.com/en-us/hotels/radisson-collection-resort-sochi-spa/rooms',
 'zapolyarie_beach':'https://zapolyarie-sochi.ru/coast/',
 'san_yug_beach':'https://san-yug.ru/infrastruktura/plyazh/',
 'pharmacy_gagarina':'https://apteka.ru/sochi/pharmacy/5e328069f5a9ae0001417666/',
 'coocoorooza_operator':'https://coocooroozasochi.com/about/',
 'lavicon_operator':'https://lavicon.ru/fregat-hotel/',
 'akvaloo_operator':'https://akvaloo.ru/',
 'baus_operator':'https://manterasupreme.ru/restaurants/baus-bar',
 'mantera_lobby':'https://manterasupreme.ru/restaurants/lobby-bar-mantera',
}

def main():
    out=BASE/'validation/sources';out.mkdir(parents=True,exist_ok=True);ledger=[]
    old_path=out/'source_ledger.json'
    old={r['source_id']:r for r in json.loads(old_path.read_text())} if old_path.exists() else {}
    for key,url in URLS.items():
        host=urlparse(url).hostname
        assert host and not any(x in host for x in ['ostrovok','yandex','google','2gis','booking.','tripadvisor'])
        target=out/(key+('.csv' if url.endswith('.csv') else '.html'))
        rec={'source_id':key,'url':url,'fetched_at':datetime.now(timezone.utc).isoformat(),
             'purpose':'independent validation evidence; not an automatically licensed production dataset'}
        if target.exists():
            b=target.read_bytes();rec=old.get(key,rec);rec['reuse_cache']=True
        else:
            p=subprocess.run(['curl','-L','--max-time','25','--max-redirs','5','-sS',url,
                              '-o',str(target),'-w','%{http_code}\t%{url_effective}'],capture_output=True,text=True)
            parts=p.stdout.split('\t',1);rec.update(curl_exit=p.returncode,http_status=parts[0] if parts else None,
                                                   final_url=parts[1] if len(parts)>1 else None,error=p.stderr or None)
            b=target.read_bytes() if target.exists() else b''
        rec.update(path=str(target),bytes=len(b),sha256=hashlib.sha256(b).hexdigest())
        ledger.append(rec)
        (out/'source_ledger.json').write_text(json.dumps(ledger,ensure_ascii=False,indent=2))
        print(key,rec.get('http_status'),len(b),flush=True)

if __name__=='__main__':main()

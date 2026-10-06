"""Fetch OFL fonts and licenses from one pinned Google Fonts revision."""
import json,re
from pathlib import Path
from urllib.parse import quote
import httpx

root=Path(__file__).resolve().parents[1]
fonts={'cormorantgaramond':('cormorantgaramond','normal'),'cormorantitalic':('cormorantgaramond','italic'),'prata':('prata','normal'),'tenorsans':('tenorsans','normal'),'poiretone':('poiretone','normal'),'greatvibes':('greatvibes','normal'),'badscript':('badscript','normal'),'unbounded':('unbounded','normal')}
with httpx.Client(timeout=30,follow_redirects=True) as client:
    r=client.get('https://api.github.com/repos/google/fonts/commits/main');r.raise_for_status();revision=r.json()['sha']
    records=[]
    for name,(folder,style) in fonts.items():
        base=f'https://raw.githubusercontent.com/google/fonts/{revision}/ofl/{folder}/'
        r=client.get(base+'METADATA.pb');r.raise_for_status();metadata=r.text
        assert 'subsets: "cyrillic"' in metadata and 'license: "OFL"' in metadata
        blocks=re.findall(r'fonts \{(.*?)\n\}',metadata,re.S)
        match=next(b for b in blocks if f'style: "{style}"' in b)
        filename=re.search(r'filename: "(.*?)"',match)[1]
        r=client.get(base+quote(filename));r.raise_for_status();payload=r.content
        (root/'backend/fonts'/f'{name}.ttf').write_bytes(payload)
        r=client.get(base+'OFL.txt');r.raise_for_status()
        (root/'backend/fonts'/f'{name}-OFL.txt').write_text(r.text)
        records.append({'id':name,'google_fonts_folder':folder,'filename':filename,'bytes':len(payload),'source':base+quote(filename)})
        print(name,len(payload))
    (root/'docs/extra-font-sources.json').write_text(json.dumps({'revision':revision,'fonts':records},indent=2)+'\n')

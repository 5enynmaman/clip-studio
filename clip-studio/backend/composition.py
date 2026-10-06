"""Shared canvas geometry and SDR layers used by browser and MP4 export."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import store

FONT_DIR = Path(__file__).parent/'fonts'
FONTS = ('manrope','montserrat','russoone','lora','robotomono','cormorantgaramond','cormorantitalic','prata','tenorsans','poiretone','greatvibes','badscript','unbounded')


def font(size, name='manrope'):
    result = ImageFont.truetype(str(FONT_DIR/f'{name}.ttf'), max(1,size))
    try:
        axes = result.get_variation_axes()
        result.set_variation_by_axes([min(a['maximum'],max(a['minimum'],700)) for a in axes])
    except OSError:
        pass
    return result


def default_boxes(style, width, height, base):
    scale=base/1080
    border=round(style.border*scale)//2*2
    header=min(round(style.header_height*scale),int(height*.45))//2*2 if style.layout=='card' else 0
    footer=round(100*scale)//2*2 if style.layout=='card' else 0
    return {'video':{'x':border/width,'y':(border+header)/height,'w':(width-2*border)/width,'h':(height-2*border-header-footer)/height},
            'header':{'x':0,'y':0,'w':1,'h':(border+header)/height if header else .2}}


def box(style, name, width, height, base):
    value=getattr(style,name+'_box',None)
    if value:
        return value.model_dump()
    defaults=default_boxes(style,width,height,base)
    if name in defaults:
        return defaults[name]
    header=box(style,'header',width,height,base)
    scale=base/1080
    if name=='title':
        if style.layout=='card':
            return bounded({'x':header['x']+header['w']*.06,'y':header['y']+(90 if style.eyebrow else 24)*scale/height,'w':header['w']*.88,'h':max(.02,header['h']-(110 if style.eyebrow else 44)*scale/height)})
        y={'top':.12,'center':.45,'bottom':.68}[style.title_position]
        return {'x':.06,'y':y,'w':.88,'h':.18}
    if name=='eyebrow':
        return bounded({'x':header['x']+header['w']*.06,'y':header['y']+25*scale/height,'w':header['w']*.88,'h':40*scale/height})
    return {'x':.06,'y':1-(style.border+(67 if style.layout=='card' else 90))*scale/height,'w':.88,'h':50*scale/height}


def bounded(b):
    b['w']=max(.03,min(1,b['w']));b['h']=max(.02,min(1,b['h']))
    b['x']=max(0,min(1-b['w'],b['x']));b['y']=max(0,min(1-b['h'],b['y']))
    return b


def geometry(style,width,height,base):
    b=box(style,'video',width,height,base)
    x,y,w,h=[round(b[k]*extent)//2*2 for k,extent in [('x',width),('y',height),('w',width),('h',height)]]
    return x,y,max(2,min(w,width-x)),max(2,min(h,height-y))


def draw_text(image,style,name,text,width,height,base):
    if not text:
        return
    b=box(style,name,width,height,base)
    x,y=round(b['x']*width),round(b['y']*height)
    available_w,available_h=max(10,round(b['w']*width)),max(10,round(b['h']*height))
    size=round((style.font_size if name=='title' else 32 if name=='eyebrow' else 36)*base/1080)
    # Measure with the exact bundled font, also used by the browser.
    while True:
        chosen=font(size,style.font)
        lines=[]
        for line in text.split('\n'):
            current=''
            for word in line.split():
                candidate=(current+' '+word).strip()
                if chosen.getlength(candidate)>available_w and current:
                    lines.append(current); current=''
                while chosen.getlength(word)>available_w and len(word)>1:
                    cut=1
                    while cut<len(word) and chosen.getlength(word[:cut+1])<=available_w:
                        cut+=1
                    if current: lines.append(current);current=''
                    lines.append(word[:cut]);word=word[cut:]
                current=(current+' '+word).strip()
            lines.append(current)
        if len(lines)*size*1.2<=available_h or size<=max(10,round(16*base/1080)):
            break
        size-=1
    layer=Image.new('RGBA',(available_w,available_h))
    draw=ImageDraw.Draw(layer)
    if style.layout=='plain' and name in {'title','account'}:
        draw.rounded_rectangle((0,0,available_w,available_h),radius=8,fill='#101116aa')
    centered=name=='account' or (style.layout=='plain' and name=='title')
    draw.multiline_text((available_w/2 if centered else 0,0),'\n'.join(lines),font=chosen,fill=style.text_color,spacing=round(size*.2),anchor='mt' if centered else 'lt',align='center' if centered else 'left')
    image.alpha_composite(layer,(x,y))


def overlay(style,width,height,base,path):
    x,y,inner_w,inner_h=geometry(style,width,height,base)
    image=Image.new('RGBA',(width,height),style.background)
    draw=ImageDraw.Draw(image)
    radius=min(round(style.corner_radius*base/1080),inner_w//2,inner_h//2)
    draw.rounded_rectangle((x,y,x+inner_w-1,y+inner_h-1),radius=radius,fill=(0,0,0,0))
    if style.layout=='card':
        b=box(style,'header',width,height,base)
        draw.rectangle((round(b['x']*width),round(b['y']*height),round((b['x']+b['w'])*width)-1,round((b['y']+b['h'])*height)-1),fill=style.background)
    if style.layout=='card' and style.header_image:
        file=store.DATA/'graphics'/f'{style.header_image}.webp'
        if not file.is_file():
            raise ValueError('Изображение шапки не найдено. Загрузите его заново')
        b=box(style,'header',width,height,base)
        photo=ImageOps.fit(Image.open(file).convert('RGBA'),(max(1,round(b['w']*width)),max(1,round(b['h']*height))))
        image.alpha_composite(photo,(round(b['x']*width),round(b['y']*height)))
    if style.layout=='card':
        draw_text(image,style,'eyebrow',style.eyebrow,width,height,base)
    draw_text(image,style,'title',style.title,width,height,base)
    draw_text(image,style,'account',style.account,width,height,base)
    image.save(path)

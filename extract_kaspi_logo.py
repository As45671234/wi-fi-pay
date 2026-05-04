import urllib.request, urllib.parse

req = urllib.request.Request('https://kaspi.kz/pay/Buslink', headers={'User-Agent':'Mozilla/5.0'})
html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8','ignore')
idx = html.find('data:image/svg+xml;charset=utf-8,')
if idx >= 0:
    raw = html[idx:]
    end = raw.find('")')
    data_url = raw[:end]
    decoded = urllib.parse.unquote(data_url.replace('data:image/svg+xml;charset=utf-8,',''))
    print(decoded)
else:
    print('NOT FOUND')

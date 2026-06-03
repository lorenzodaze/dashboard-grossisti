import os
import json
import base64
import requests
import calendar
from datetime import datetime, date
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERATIONS = 200000


def encrypt_json(obj, password):
    """Cifra un oggetto JSON con AES-256-GCM; chiave derivata dalla password
    via PBKDF2-SHA256. Compatibile con la Web Crypto API del browser."""
    salt = os.urandom(16)
    kdf  = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=PBKDF2_ITERATIONS)
    key  = kdf.derive(password.encode('utf-8'))
    iv   = os.urandom(12)
    data = json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
    ct   = AESGCM(key).encrypt(iv, data, None)
    return {
        'v':    1,
        'iter': PBKDF2_ITERATIONS,
        'salt': base64.b64encode(salt).decode(),
        'iv':   base64.b64encode(iv).decode(),
        'ct':   base64.b64encode(ct).decode(),
    }

CLIENT_ID     = os.environ.get('ZOHO_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET', '')
REFRESH_TOKEN = os.environ.get('ZOHO_REFRESH_TOKEN', '')
BASE_URL  = 'https://www.zohoapis.eu/crm/v7'
AUTH_URL  = 'https://accounts.zoho.eu/oauth/v2/token'


def get_token():
    r = requests.post(AUTH_URL, data={
        'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN, 'grant_type': 'refresh_token'
    })
    r.raise_for_status()
    return r.json()['access_token']


def auth(token):
    return {'Authorization': f'Zoho-oauthtoken {token}'}


def search_records(token, module, criteria, fields):
    results, page = [], 1
    while True:
        r = requests.get(f'{BASE_URL}/{module}/search',
            headers=auth(token),
            params={'criteria': criteria, 'fields': fields, 'per_page': 200, 'page': page})
        if r.status_code == 204:
            break
        if not r.ok:
            print(f"  search error {r.status_code} on {module}: {r.text[:200]}")
            break
        d = r.json()
        if 'data' not in d:
            break
        results.extend(d['data'])
        if not d.get('info', {}).get('more_records'):
            break
        page += 1
    return results


def get_order_items(token, order_id):
    """Fetch a single Sales Order and return its embedded Ordered_Items list."""
    r = requests.get(f'{BASE_URL}/Sales_Orders/{order_id}', headers=auth(token))
    if not r.ok:
        return []
    d = r.json()
    if 'data' not in d or not d['data']:
        return []
    return d['data'][0].get('Ordered_Items', [])


def get_all_records(token, module, fields, per_page=200):
    """Pagina su tutti i record del modulo via /module (non /search).
    Supporta page_token, quindi nessun limite a 2000 record."""
    results = []
    page_token = None
    while True:
        params = {'fields': fields, 'per_page': per_page}
        if page_token:
            params['page_token'] = page_token
        r = requests.get(f'{BASE_URL}/{module}', headers=auth(token), params=params)
        if r.status_code == 204:
            break
        if not r.ok:
            print(f"  get_all error {r.status_code} on {module}: {r.text[:200]}")
            break
        d = r.json()
        if 'data' not in d:
            break
        results.extend(d['data'])
        info = d.get('info', {}) or {}
        page_token = info.get('next_page_token')
        if not page_token or not info.get('more_records'):
            break
    return results


def quarter_of(d):
    return (d.month - 1) // 3 + 1, d.year


def qlabel(q, y):
    return f"Q{q} {y}"


# Province della Lombardia orientale; le restanti finiscono in "Lombardia Ovest"
LOMBARDIA_EST = {'Bergamo', 'Brescia', 'Cremona', 'Mantova'}
# Regioni raggruppate sotto "Triveneto"
TRIVENETO = {'Veneto', 'Friuli-Venezia Giulia', 'Trentino-Alto Adige'}


def remap_region(region, province):
    region   = (region or '').strip()
    province = (province or '').strip()
    if region == 'Lombardia':
        return 'Lombardia Est' if province in LOMBARDIA_EST else 'Lombardia Ovest'
    if region in TRIVENETO:
        return 'Triveneto'
    return region


def main():
    today = date.today()
    token = get_token()
    print("Token OK")

    # --- Wholesaler accounts ---
    print("Fetching wholesaler accounts...")
    accounts = search_records(token, 'Accounts',
        '(Client_type:equals:Wholesaler)', 'id,Account_Name,Country,Region,State_Province')
    print(f"Wholesaler accounts: {len(accounts)}")
    wholesaler_ids = {a['id'] for a in accounts}
    acct_info      = {a['id']: {
        'name':    a.get('Account_Name', '') or '',
        'country': a.get('Country', '') or '',
        'region':  remap_region(a.get('Region', ''), a.get('State_Province', '')),
    } for a in accounts}

    if not wholesaler_ids:
        print("No wholesaler accounts found.")
        return

    # --- Catalogo prodotti: mappa codice -> categoria ---
    print("Fetching product catalog...")
    products_all = get_all_records(token, 'Products', 'id,Product_Code,Product_Category')
    product_group = {}
    for p in products_all:
        code = (p.get('Product_Code') or '').strip()
        cat  = (p.get('Product_Category') or 'Altri').strip() or 'Altri'
        if code:
            product_group[code] = cat
    print(f"Products in catalog: {len(product_group)}")

    # --- Sales Orders: cerca MESE PER MESE (l'endpoint /search e' limitato a
    #     ~2000 record per query, quindi una singola ricerca su 2 anni taglierebbe
    #     via lo storico piu' vecchio). Si parte da gennaio di 2 anni fa.
    order_fields = ('id,SO_Number,Account_Name,Date,Shipping_Date,'
                    'Sub_Total,Checkout_Discount,Checkout_discount_value')
    start_year = 2025
    print(f"Fetching orders month by month from {start_year}-01 to {today.strftime('%Y-%m')}...")
    all_orders = []
    y, m = start_year, 1
    while (y, m) <= (today.year, today.month):
        first    = date(y, m, 1)
        last_day = calendar.monthrange(y, m)[1]
        last     = min(date(y, m, last_day), today)
        chunk = search_records(token, 'Sales_Orders',
            f'((Date:between:{first:%Y-%m-%d},{last:%Y-%m-%d})and(Order_Type:equals:Sales))',
            order_fields)
        all_orders.extend(chunk)
        if chunk:
            print(f"  {y}-{m:02d}: {len(chunk)} orders")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    print(f"Total orders in period: {len(all_orders)}")

    # Filtra solo gli ordini dei clienti Wholesaler
    orders_raw = []
    for o in all_orders:
        acct = o.get('Account_Name', {})
        if isinstance(acct, dict) and acct.get('id') in wholesaler_ids:
            info = acct_info.get(acct.get('id', ''), {})
            o['_account_id']      = acct.get('id', '')
            o['_account_name']    = info.get('name') or acct.get('name', '')
            o['_account_country'] = info.get('country', '')
            o['_account_region']  = info.get('region', '')
            orders_raw.append(o)
    print(f"Wholesaler orders: {len(orders_raw)}")

    # --- Ordered Items: embedded nel record ordine, fetch individuale ---
    print(f"Fetching full order records for {len(orders_raw)} orders...")
    items_by_order = {}
    for i, order in enumerate(orders_raw):
        oid = order['id']
        items_by_order[oid] = get_order_items(token, oid)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(orders_raw)} orders processed")
    print(f"Items loaded for {len(items_by_order)} orders")

    # --- Quarters list ---
    cq, cy = quarter_of(today)
    quarters = []
    q, y = cq, cy
    for _ in range(5):
        quarters.append(qlabel(q, y))
        q -= 1
        if q == 0:
            q, y = 4, y - 1

    current_month = today.strftime('%Y-%m')

    # --- Struttura per cliente ---
    clients = {}
    for order in orders_raw:
        aid   = order['_account_id']
        aname = order['_account_name']

        date_str = order.get('Date', '')
        # Use Shipping_Date for monthly/quarterly allocation; fall back to Date
        ship_str  = order.get('Shipping_Date') or ''
        alloc_str = ship_str if ship_str else date_str
        if not alloc_str:
            continue
        try:
            odate = datetime.strptime(alloc_str, '%Y-%m-%d').date()
        except ValueError:
            continue

        # Net value: Sub_Total (after line discounts) minus cash discount, no VAT
        sub_total     = float(order.get('Sub_Total', 0) or 0)
        checkout_disc = float(order.get('Checkout_discount_value', 0) or 0)
        checkout_pct  = float(order.get('Checkout_Discount', 0) or 0)
        total         = round(sub_total - checkout_disc, 2)

        omonth   = odate.strftime('%Y-%m')
        oq, oy   = quarter_of(odate)
        oqlbl    = qlabel(oq, oy)

        if aid not in clients:
            clients[aid] = {
                'id':      aid,
                'name':    aname,
                'country': order.get('_account_country', ''),
                'region':  order.get('_account_region', ''),
                'quarterly': {}, 'monthly': {}, 'orders': []
            }

        c = clients[aid]
        c['monthly'][omonth] = c['monthly'].get(omonth, 0) + total
        c['quarterly'][oqlbl] = c['quarterly'].get(oqlbl, 0) + total

        items_out = []
        for it in items_by_order.get(order['id'], []):
            pname = it.get('Product_Name', '')
            pname = pname.get('name', '') if isinstance(pname, dict) else str(pname)
            item_net   = float(it.get('Net_Total',      0) or 0)
            item_unit  = float(it.get('Unitary_Price_1', 0) or 0)
            # Apply cash discount proportionally at item level
            disc_factor = 1 - checkout_pct / 100 if checkout_pct else 1
            code = it.get('Product_Code', '') or ''
            items_out.append({
                'code':  code,
                'name':  pname,
                'group': product_group.get(code, 'Altri'),
                'qty':   float(it.get('Quantity', 0) or 0),
                'unit':  round(item_unit * disc_factor, 4),
                'total': round(item_net  * disc_factor, 2),
            })

        c['orders'].append({
            'id':    order['id'],
            'num':   order.get('SO_Number', ''),
            'date':  date_str,
            'ship':  ship_str,
            'total': total,
            'items': items_out,
        })

    # La dashboard ricalcola tutto lato browser dai dati per-cliente, quindi
    # l'output contiene solo i metadati di periodo e l'elenco clienti.
    def build_output(client_list):
        return {
            'generated_at':        datetime.now().strftime('%d/%m/%Y %H:%M'),
            'current_month':       current_month,
            'current_month_label': today.strftime('%B %Y').capitalize(),
            'quarters':            quarters,
            'clients':             client_list,
        }

    all_clients       = list(clients.values())
    triveneto_clients = [c for c in all_clients if c.get('region') == 'Triveneto']
    lazio_clients     = [c for c in all_clients if c.get('region') == 'Lazio']

    full_output = build_output(all_clients)
    triv_output = build_output(triveneto_clients)
    lazio_output = build_output(lazio_clients)

    pwd_a = os.environ.get('DASH_PASSWORD_A', '')
    pwd_b = os.environ.get('DASH_PASSWORD_B', '')
    pwd_c = os.environ.get('DASH_PASSWORD_C', '')
    if not pwd_a or not pwd_b or not pwd_c:
        raise SystemExit("ERRORE: imposta i secret DASH_PASSWORD_A, DASH_PASSWORD_B e DASH_PASSWORD_C.")

    os.makedirs('data', exist_ok=True)
    with open('data/full.enc', 'w', encoding='utf-8') as f:
        json.dump(encrypt_json(full_output, pwd_a), f)
    with open('data/triveneto.enc', 'w', encoding='utf-8') as f:
        json.dump(encrypt_json(triv_output, pwd_b), f)
    with open('data/lazio.enc', 'w', encoding='utf-8') as f:
        json.dump(encrypt_json(lazio_output, pwd_c), f)

    print(f"\nSaved data/full.enc ({len(all_clients)} clienti), "
          f"data/triveneto.enc ({len(triveneto_clients)} clienti) e "
          f"data/lazio.enc ({len(lazio_clients)} clienti)")


if __name__ == '__main__':
    main()

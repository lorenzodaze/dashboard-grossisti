import os
import json
import requests
import calendar
from datetime import datetime, date

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


def quarter_of(d):
    return (d.month - 1) // 3 + 1, d.year


def qlabel(q, y):
    return f"Q{q} {y}"


def main():
    today = date.today()
    token = get_token()
    print("Token OK")

    # --- Wholesaler accounts ---
    print("Fetching wholesaler accounts...")
    accounts = search_records(token, 'Accounts',
        '(Client_type:equals:Wholesaler)', 'id,Account_Name,Country,Region')
    print(f"Wholesaler accounts: {len(accounts)}")
    wholesaler_ids = {a['id'] for a in accounts}
    acct_info      = {a['id']: {
        'name':    a.get('Account_Name', '') or '',
        'country': a.get('Country', '') or '',
        'region':  a.get('Region', '') or '',
    } for a in accounts}

    if not wholesaler_ids:
        print("No wholesaler accounts found.")
        return

    # --- Sales Orders: cerca per data (ultimi 2 anni) poi filtra per account ---
    two_years_ago = date(today.year - 2, today.month, 1).strftime('%Y-%m-%d')
    today_str     = today.strftime('%Y-%m-%d')

    print(f"Fetching all orders from {two_years_ago} to {today_str}...")
    all_orders = search_records(token, 'Sales_Orders',
        f'(Date:between:{two_years_ago},{today_str})',
        'id,SO_Number,Account_Name,Date,Shipping_Date,Sub_Total,Checkout_Discount,Checkout_discount_value')
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
            items_out.append({
                'code':  it.get('Product_Code', '') or '',
                'name':  pname,
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

    # --- Grafico mensile (ultimi 12 mesi) ---
    monthly_chart = {}
    for i in range(11, -1, -1):
        year  = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year  -= 1
        m_str = f"{year:04d}-{month:02d}"
        label = date(year, month, 1).strftime('%b %Y')
        monthly_chart[label] = round(
            sum(c['monthly'].get(m_str, 0) for c in clients.values()), 2)

    # --- Riepilogo ---
    summary_q, summary_month = {}, 0
    for c in clients.values():
        summary_month += c['monthly'].get(current_month, 0)
        for ql, t in c['quarterly'].items():
            summary_q[ql] = summary_q.get(ql, 0) + t

    # --- Totali prodotti ---
    products = {}
    for c in clients.values():
        for o in c['orders']:
            for it in o['items']:
                code = it['code'] or 'N/D'
                if code not in products:
                    products[code] = {'code': code, 'name': it['name'], 'qty': 0.0, 'total': 0.0}
                products[code]['qty']   += it['qty']
                products[code]['total'] += it['total']

    output = {
        'generated_at':        datetime.now().strftime('%d/%m/%Y %H:%M'),
        'current_month':       current_month,
        'current_month_label': today.strftime('%B %Y').capitalize(),
        'current_quarter':     quarters[0] if quarters else '',
        'quarters':            quarters,
        'summary':             {'month': round(summary_month, 2), 'quarterly': summary_q},
        'monthly_chart':       monthly_chart,
        'clients':             list(clients.values()),
        'products':            sorted(products.values(), key=lambda x: -x['total']),
    }

    os.makedirs('data', exist_ok=True)
    with open('data/dashboard.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\nSaved data/dashboard.json")
    print(f"Clients : {len(clients)}")
    print(f"Products: {len(products)}")
    print(f"Month total  : €{summary_month:,.2f}")
    if quarters:
        print(f"Quarter total: €{summary_q.get(quarters[0], 0):,.2f}")


if __name__ == '__main__':
    main()

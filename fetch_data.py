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
    """Zoho CRM search endpoint — non richiede scope COQL"""
    results, page = [], 1
    while True:
        r = requests.get(f'{BASE_URL}/{module}/search',
            headers=auth(token),
            params={'criteria': criteria, 'fields': fields, 'per_page': 200, 'page': page})
        if r.status_code == 204:
            break
        if not r.ok:
            print(f"  search error {r.status_code}: {r.text[:200]}")
            break
        d = r.json()
        if 'data' not in d:
            break
        results.extend(d['data'])
        if not d.get('info', {}).get('more_records'):
            break
        page += 1
    return results


def get_related(token, module, record_id, related, fields):
    """Related records endpoint"""
    results, page = [], 1
    while True:
        r = requests.get(f'{BASE_URL}/{module}/{record_id}/{related}',
            headers=auth(token),
            params={'fields': fields, 'per_page': 200, 'page': page})
        if r.status_code == 204:
            break
        if not r.ok:
            break
        d = r.json()
        if 'data' not in d:
            break
        results.extend(d['data'])
        if not d.get('info', {}).get('more_records'):
            break
        page += 1
    return results


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
        '(Client_type:equals:Wholesaler)', 'id,Account_Name')
    print(f"Wholesaler accounts: {len(accounts)}")

    if not accounts:
        print("No wholesaler accounts found.")
        return

    # --- Sales Orders per account ---
    print("Fetching sales orders...")
    orders_raw = []
    for acct in accounts:
        aid   = acct['id']
        aname = acct['Account_Name']
        orders = get_related(token, 'Accounts', aid, 'Sales_Orders',
            'id,SO_Number,Account_Name,Date,Grand_Total')
        for o in orders:
            o['_account_id']   = aid
            o['_account_name'] = aname
        orders_raw.extend(orders)
        print(f"  {aname}: {len(orders)} orders")

    print(f"Total orders: {len(orders_raw)}")

    # --- Ordered Items per order ---
    print("Fetching ordered items...")
    items_by_order = {}
    for order in orders_raw:
        oid   = order['id']
        items = get_related(token, 'Sales_Orders', oid, 'Ordered_Items',
            'Product_Name,Product_Code,Quantity,Net_Total,Net_price_1')
        items_by_order[oid] = items
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

    # --- Per-client structure ---
    clients = {}
    for order in orders_raw:
        aid   = order['_account_id']
        aname = order['_account_name']

        date_str = order.get('Date', '')
        if not date_str:
            continue
        try:
            odate = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            continue

        total    = float(order.get('Grand_Total', 0) or 0)
        omonth   = odate.strftime('%Y-%m')
        oq, oy   = quarter_of(odate)
        oqlabel  = qlabel(oq, oy)

        if aid not in clients:
            clients[aid] = {
                'id': aid, 'name': aname,
                'quarterly': {}, 'monthly': {}, 'orders': []
            }

        c = clients[aid]
        c['monthly'][omonth]    = c['monthly'].get(omonth, 0)    + total
        c['quarterly'][oqlabel] = c['quarterly'].get(oqlabel, 0) + total

        items_out = []
        for it in items_by_order.get(order['id'], []):
            pname = it.get('Product_Name', '')
            pname = pname.get('name', '') if isinstance(pname, dict) else str(pname)
            items_out.append({
                'code':  it.get('Product_Code', '') or '',
                'name':  pname,
                'qty':   float(it.get('Quantity',    0) or 0),
                'unit':  float(it.get('Net_price_1', 0) or 0),
                'total': float(it.get('Net_Total',   0) or 0),
            })

        c['orders'].append({
            'id':    order['id'],
            'num':   order.get('SO_Number', ''),
            'date':  date_str,
            'total': total,
            'items': items_out,
        })

    # --- Monthly chart (last 12 months) ---
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

    # --- Summary ---
    summary_q, summary_month = {}, 0
    for c in clients.values():
        summary_month += c['monthly'].get(current_month, 0)
        for ql, t in c['quarterly'].items():
            summary_q[ql] = summary_q.get(ql, 0) + t

    # --- Products totals ---
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

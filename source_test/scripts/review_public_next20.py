"""Question-specific independent audit of frozen baseline answers 11--30."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from scripts.review_public_first10 import (
    clean, csv_rows, day, deduplicate, excel, grouped, invalid_status,
    key, number, plain, quantity,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/public_test/data"
OUT = ROOT / "artifacts/public_next20"


def rounded(value, places=0):
    value = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(value) if places == 0 else float(value)


def sale_rows(bid, filename, sheet="DonHang_ChiTiet", header=4):
    return deduplicate(grouped(excel(DATA / bid / filename, sheet, header),
        "Mã đơn hàng", "Tên sản phẩm", ("Ngày đặt hàng", "Trạng thái đơn hàng", "Tên khách hàng")))


def eligible_sales(bid, filenames, extra_bad=()):
    bad = set(extra_bad)
    rows = []
    for filename in filenames:
        for sheet, line, row in sale_rows(bid, filename):
            if invalid_status(row["Trạng thái đơn hàng"]):
                bad.add(clean(row["Mã đơn hàng"]))
            rows.append((filename, sheet, line, row))
    return [record for record in rows if clean(record[3]["Mã đơn hàng"]) not in bad]


def audit_dev002():
    rows = eligible_sales("dev_002", ["BanHang_T9_2025.xlsx", "BanHang_T1_2025.xlsx", "BanHang_T6_2025.xlsx"])
    result = {}
    for qid, start, end, average in [
        ("q_03bbdfa6e0d5", "2025-09-09", "2025-09-10", False),
        ("q_040d437fdf09", "2025-01-01", "2025-02-01", True),
        ("q_04e55a8f8c0c", "2025-06-01", "2025-07-01", True),
    ]:
        selected = [r for r in rows if start <= day(r[3]["Ngày đặt hàng"]).isoformat() < end]
        orders = {clean(r[3]["Mã đơn hàng"]) for r in selected}
        total = sum((number(r[3]["Tổng tiền"]) for r in selected), Decimal(0))
        result[qid] = {"expected": rounded(total / len(orders)) if average else len(orders),
            "revenue": str(total), "orders": len(orders), "valid_lines": len(selected), "window": [start, end]}
    return result


def audit_dev005():
    bid = "dev_005"
    bad = {clean(r["Mã đơn hàng"]) for _, _, r in excel(DATA / bid / "DonHang_Lo.xlsx", "Sheet1")
           if r.get("Số lượng trả") is not None and quantity(r["Số lượng trả"]) > 0}
    files = ["BanHang_Thang10_2025.xlsx", *[f"BanHang_Thang{m:02d}_2026.xlsx" for m in [1, 2, 3, 5]]]
    rows = eligible_sales(bid, files, bad)
    year_rows = [r for r in rows if "2026-01-01" <= day(r[3]["Ngày đặt hàng"]).isoformat() < "2026-03-17"]
    october = [r for r in rows if "2025-10-01" <= day(r[3]["Ngày đặt hàng"]).isoformat() < "2025-11-01"]
    rx_codes = {clean(r["Mã SKU"]) for _, _, r in excel(DATA / bid / "DanhSachThuoc.xlsx", "Sheet1")
                if plain(r.get("Thuốc kê đơn")) in {"true", "1"} or r.get("Thuốc kê đơn") is True}
    rx_names = {plain(r["Tên sản phẩm"]) for _, _, r in excel(DATA / bid / "DanhSachSanPham.xlsx", "Sheet1")
                if clean(r["Mã SKU"]) in rx_codes}
    rx_rows = [r for r in rows if "2026-05-01" <= day(r[3]["Ngày đặt hàng"]).isoformat() < "2026-05-11"
               and plain(r[3]["Tên sản phẩm"]) in rx_names]
    return {
        "q_037178b58fcc": {"expected": len({clean(r[3]["Mã đơn hàng"]) for r in year_rows}), "valid_lines": len(year_rows)},
        "q_03bee605bf7f": {"expected": rounded(sum((number(r[3]["Tổng tiền"]) for r in october), Decimal(0))), "valid_lines": len(october)},
        "q_05421814d0e3": {"expected": rounded(sum((number(r[3]["Tổng tiền"]) for r in rx_rows), Decimal(0))), "valid_lines": len(rx_rows),
            "daily_revenue": {d: str(sum((number(r[3]["Tổng tiền"]) for r in rx_rows if day(r[3]["Ngày đặt hàng"]).isoformat()==d), Decimal(0)))
                              for d in sorted({day(r[3]["Ngày đặt hàng"]).isoformat() for r in rx_rows})}},
    }


def audit_dev006():
    path = DATA / "dev_006/pos.sqlite"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    bad = {(r["IDCuaHang"], r["MaHoaDon"]) for r in db.execute("SELECT * FROM tra_hang")}
    orders = {(r["IDCuaHang"], r["MaHoaDon"]): dict(r) for r in db.execute("SELECT * FROM don_hang")
              if not invalid_status(r["TinhTrang"]) and (r["IDCuaHang"], r["MaHoaDon"]) not in bad}
    revenues = defaultdict(Decimal)
    lines = set()
    for row in db.execute("SELECT * FROM chi_tiet_don"):
        signature = tuple(row)
        if signature in lines:
            continue
        lines.add(signature)
        oid = row["IDCuaHang"], row["MaHoaDon"]
        if oid in orders:
            revenues[oid] += number(row["TriGia"])
    def revenue(start, end, shift=None):
        total = Decimal(0)
        for oid, order in orders.items():
            d = day(order["NgayLap"]).isoformat()
            h = int(order["ThoiGianLap"][11:13])
            if start <= d < end and (shift is None or shift[0] <= h < shift[1]):
                total += revenues[oid]
        return total
    pre = revenue("2026-01-24", "2026-02-10")
    tet = revenue("2026-02-10", "2026-02-27")
    customers = defaultdict(set)
    for oid, order in orders.items():
        if "2026-02-01" <= day(order["NgayLap"]).isoformat() < "2026-03-01" and clean(order["MaKhachHang"]):
            customers[(oid[0], clean(order["MaKhachHang"]))].add(oid)
    morning = revenue("2025-10-01", "2025-11-01", (6, 14))
    afternoon = revenue("2025-10-01", "2025-11-01", (14, 22))
    purchases = defaultdict(Decimal)
    suppliers = {(r["IDCuaHang"], r["MaNhaCungCap"]): r["TenGoi"] for r in db.execute("SELECT * FROM nha_cung_cap")}
    for row in db.execute("SELECT * FROM phieu_nhap"):
        if "2026-08-01" <= day(row["NgayNhapKho"]).isoformat() < "2026-08-09" and not invalid_status(row["TinhTrang"]):
            purchases[(row["IDCuaHang"], row["MaNhaCungCap"])] += number(row["TongGiaTriNhap"])
    winner = sorted(purchases, key=lambda code: (-purchases[code], code))[0]
    db.close()
    return {
        "q_0389b0c41da6": {"expected": rounded((tet-pre)*100/pre, 2), "tet_revenue": str(tet), "before_revenue": str(pre),
            "calendar_window_to_verify": ["2026-02-10", "2026-02-27"]},
        "q_049f0a34efdc": {"expected": sum(len(ids)>=4 for ids in customers.values()), "customer_order_counts": {str(k): len(v) for k,v in customers.items()}},
        "q_052834325528": {"expected": rounded((afternoon-morning)*100/morning, 2), "morning": str(morning), "afternoon": str(afternoon)},
        "q_0641a621b596": {"expected": [rounded(revenue(f"2025-{m:02d}-01", f"2025-{m+1:02d}-01")) for m in [4,5,6]]},
        "q_0878814b6a76": {"expected": suppliers[winner], "supplier_code": winner, "ranking": {suppliers[k]: str(v) for k,v in purchases.items()}},
    }


def customer_name(value):
    text = clean(value)
    text = re.sub(r"[\s(,–-]*(?:\+?84|0)(?:[ .-]*\d){8,10}\)?\s*$", "", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).casefold().split())


def worker_alias(value):
    # Honorific separators can be spaces OR punctuation: A.An and a An
    # are recorded spellings of the same worker, not two different people.
    return re.sub(r'^(?:anh|chu|bac|tho|a)[.\s]+', '', plain(value))


def audit_dev001():
    rows = list(deduplicate(grouped(excel(DATA / "dev_001/So_chi_tiet_ban_hang.xlsx", "Sheet1", 4),
        "Mã đơn hàng", "Tên hàng hóa", ("Ngày", "Trạng thái", "Khách hàng"))))
    bad = {clean(r["Mã đơn hàng"]) for _,_,r in rows if invalid_status(r["Trạng thái"])}
    valid = [(s,n,r) for s,n,r in rows if clean(r["Mã đơn hàng"]) not in bad]
    months = defaultdict(Decimal)
    ytd = set()
    aliases = defaultdict(set)
    names = defaultdict(Counter)
    april = defaultdict(set)
    retail = {"kl", "le", "khach le", "khach vang lai", "vang lai", "khachle"}
    for _,_,r in valid:
        d = day(r["Ngày"]).isoformat();oid=clean(r["Mã đơn hàng"])
        if "2025-03-01" <= d < "2025-06-01":
            months[d[:7]] += number(r["Thành tiền"])
        if "2025-01-01" <= d < "2025-09-13":ytd.add(oid)
        name=customer_name(r.get("Khách hàng"))
        if name and plain(name) not in retail:
            aliases[oid].add(name);names[name][clean(r["Khách hàng"])] += 1
            if "2025-04-01" <= d < "2025-05-01":april[name].add(oid)
    parent={name:name for name in names}
    def find(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]];x=parent[x]
        return x
    for variants in aliases.values():
        variants=sorted(variants)
        for other in variants[1:]:parent[find(other)]=find(variants[0])
    counts=defaultdict(set);display=defaultdict(Counter)
    for name,ids in april.items():counts[find(name)].update(ids);display[find(name)].update(names[name])
    ranking=sorted(counts,key=lambda name:(-len(counts[name]),name))
    winner=ranking[0]
    return {
        "q_0537f6e4f5ab": {"expected": sorted(months,key=lambda m:(-months[m],m))[0], "monthly_revenue": dict(months)},
        "q_05ab1c71dcaa": {"expected": display[winner].most_common(1)[0][0], "top_customers": [{"identity": k,"orders":len(counts[k]),"names":dict(display[k])} for k in ranking[:10]],
            "excluded_retail_aliases": sorted(retail)},
        "q_08f075b54698": {"expected": len(ytd)},
    }


def audit_dev008():
    bad=set();selected=[]
    for filename in ["ChiTietHoaDon_1.xlsx","ChiTietHoaDon_2.xlsx"]:
        rows=grouped(excel(DATA/'dev_008'/filename,'Sheet1'), 'Bill','Tên hàng',('Ngày xuất','TT đơn'))
        for _,_,r in rows:
            oid=clean(r['Bill'])
            if invalid_status(r.get('TT đơn')):bad.add(oid)
            if "2026-01-01"<=day(r['Ngày xuất']).isoformat()<"2026-02-01" and plain(r['Tên hàng'])!='phi phuc vu':
                selected.append((oid,clean(r['Ma hang'])))
    products=defaultdict(set)
    for oid,code in selected:
        if oid not in bad:products[code].add(oid)
    return {'q_06de4545fe25':{'expected':sum(len(ids)>=187 for ids in products.values()),'product_order_counts':{k:len(v) for k,v in products.items()}}}


def audit_dev013():
    totals={}
    for month in [5,6]:
        records=list(deduplicate(grouped(excel(DATA/'dev_013/BanHang_2026.xlsx',f'T{month:02d}-2026',2),
            'HĐ','Tên hàng',('Ngày','Ghi chú','Khách','Kênh'))))
        bad={clean(r['HĐ']) for _,_,r in records if invalid_status(r.get('Ghi chú'))}
        valid=[r for _,_,r in records if clean(r['HĐ']) not in bad and r.get('SL') is not None
               and day(r['Ngày'],2026).day<=30]
        totals[month]=sum((number(r['TT']) for r in valid),Decimal(0))
    return {'q_07b7be37628b':{'expected':rounded((totals[6]-totals[5])*100/totals[5],2),'revenue_june':str(totals[6]),'revenue_may_1_to_30':str(totals[5])}}


def audit_dev010():
    orders={}
    for _,_,r in csv_rows(DATA/'dev_010/don_hang.csv'):
        if clean(r['Số CT']) and clean(r['Date']) and not invalid_status(r['Xử lý']) and '2025-10-01'<=day(r['Date']).isoformat()<'2026-01-01':
            orders[clean(r['Số CT'])]=r
    total=Decimal(0);seen=set();lines=0
    rows=grouped(csv_rows(DATA/'dev_010/chi_tiet_don.csv'),'Mã bill','Mã H.hóa',())
    for _,_,r in rows:
        sig=tuple((k,clean(v)) for k,v in r.items())
        if sig in seen:continue
        seen.add(sig)
        if clean(r['Mã bill']) in orders:total+=number(r['Cộng']);lines+=1
    return {'q_08c521dda3d9':{'expected':rounded(total),'orders':len(orders),'valid_lines':lines}}


def audit_dev012():
    names=[clean(r['Họ tên']) for _,_,r in excel(DATA/'dev_012/DanhSachTho.xlsx','Sheet1')]
    target=next(n for n in names if plain(n)=='dam lan an')
    selected=[];observed=Counter()
    for _,line,r in excel(DATA/'dev_012/SoSuaXe.xlsx','T8-2026',2):
        if not clean(r.get('Thợ')) or not clean(r.get('Ngày')):continue
        worker=worker_alias(r['Thợ']);observed[plain(r['Thợ'])]+=1
        if worker not in {'an','dam lan an'}:continue
        if '2026-08-01'<=day(r['Ngày'],2026).isoformat()<'2026-09-01':
            selected.append({'row':line,'worker':r['Thợ'],'amount':number(r['Tiền'])})
    return {'q_06b7f437fcc4':{'expected':rounded(sum((r['amount'] for r in selected),Decimal(0))),
        'formal_name':target,'roster':names,'observed_worker_labels':dict(observed),'contributions':selected}}


def audit_dev003():
    source=DATA/'dev_003'
    records=deduplicate(grouped(excel(source/'BanHang_TongHop_2025.xlsx','BanHang'),
        'Đơn số','Hàng hóa',('Giờ tạo đơn','Trạng thái giao','Khách hàng (tên)','Mã khách')))
    rows=list(records);bad={clean(r['Đơn số']) for _,_,r in rows if invalid_status(r['Trạng thái giao'])}
    selected=[r for _,_,r in rows if clean(r['Đơn số']) not in bad and '2025-10-01'<=day(r['Giờ tạo đơn']).isoformat()<'2025-11-01']
    rev=sum((number(r['Thành tiền (đ)']) for r in selected),Decimal(0))
    cost=sum((quantity(r['S.L'])*number(r['Giá vốn lúc bán']) for r in selected),Decimal(0))
    expenses=[r for _,_,r in excel(source/'SoTay_ChiPhi.xlsx','Sheet1',3) if clean(r.get('Ngày mua')) and '2025-10-01'<=day(r['Ngày mua']).isoformat()<'2025-11-01']
    expense=sum((number(r['Tiền chi']) for r in expenses),Decimal(0))
    receipts=defaultdict(set);variants=defaultdict(Counter)
    rows=grouped(excel(source/'SoTay_NhapHang.xlsx','Sheet1',3),'Số phiếu','Product',('Nhà cung cấp','Ngày nhập','Lưu ý'))
    for _,_,r in rows:
        if invalid_status(r.get('Lưu ý')) or not '2025-12-01'<=day(r['Ngày nhập']).isoformat()<'2026-01-01':continue
        supplier=plain(r['Nhà cung cấp']);receipts[supplier].add(clean(r['Số phiếu']));variants[supplier][clean(r['Nhà cung cấp'])]+=1
    ranking=sorted(receipts,key=lambda s:(-len(receipts[s]),s));winner=ranking[0]
    return {'q_06dfe652d857':{'expected':rounded(rev-cost-expense),'revenue':str(rev),'cost':str(cost),'expenses':str(expense)},
        'q_08b923f3c5c5':{'expected':variants[winner].most_common(1)[0][0],'supplier_receipt_counts':{s:len(v) for s,v in receipts.items()},'aliases':{s:dict(v) for s,v in variants.items()}}}


def main():
    OUT.mkdir(exist_ok=True)
    results={}
    with ProcessPoolExecutor(max_workers=3) as pool:
        tasks={pool.submit(fn):fn.__name__ for fn in [audit_dev001,audit_dev002,audit_dev003,audit_dev005,audit_dev006,audit_dev008,audit_dev010,audit_dev012,audit_dev013]}
        for future in as_completed(tasks):
            name=tasks[future]
            try:
                data=future.result();results.update(data)
                (OUT/f'{name}.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
                print(name,{k:v['expected'] for k,v in data.items()},flush=True)
            except Exception as exc:
                results[name]={'error':repr(exc)};print(name,repr(exc),flush=True)
    (OUT/'raw_audit.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    write_report()


def write_report():
    selection=json.loads((OUT/'selection.json').read_text(encoding='utf-8'))
    raw=json.loads((OUT/'raw_audit.json').read_text(encoding='utf-8'))
    assert len(selection)==20 and set(raw)=={r['question']['id'] for r in selection}
    notes={
        'q_040d437fdf09':'Sai do dữ liệu tiền/dòng tổng trong cache cũ. Đọc lại dòng hàng gốc, loại huỷ/trả rồi chia số đơn: 70.324 đồng/đơn.',
        'q_04e55a8f8c0c':'SQL đã tránh dòng tên sản phẩm trống, nhưng tiền trong cache cũ vẫn bị đọc sai. Tính trên nguồn gốc: 69.630 đồng/đơn.',
        'q_05ab1c71dcaa':'kl là cách ghi khách lẻ, bị sót trong danh sách loại trừ của SQL. Khách lẻ không được xếp hạng. Đối chiếu tên/cách viết cùng đơn: La Minh Dương đứng đầu với 9 đơn hợp lệ.',
        'q_05421814d0e3':'Nối tên bằng TRIM còn bỏ sót cách viết tên. Giữ nguyên SQL khác, chỉ đổi điều kiện nối sang lookup cho kết quả 2.826.100. Thay riêng mốc cuối kỳ không làm thay đổi kết quả; không xác nhận lỗi ngày ở câu này.',
        'q_06b7f437fcc4':'Đối chiếu đã sửa cách nhận alias: An, a An và A.An đều thuộc cùng thợ. Tổng đúng là 25.180.500, gồm 9.402.500 từ A.An trước đây bị bộ audit bỏ sót. Đây là tiền phiếu, không phải hoa hồng. Prediction baseline vẫn là abstain.',
        'q_06dfe652d857':'Tính lãi ròng tháng 10/2025: doanh thu 258.613.500 trừ giá vốn 76.347.976 trừ mọi chi phí 220.592.000 = −38.326.476 đồng. Solver bỏ trả lời sau lỗi schema probe và partition.',
        'q_0389b0c41da6':'Đã đối chiếu calendar.csv: 23 tháng Chạp là 10/02/2026; mùng 10 tháng Giêng là 26/02/2026. Tết gồm 17 ngày; kỳ trước là 24/01–09/02. Tính tiền từ POS gốc, không lấy lịch bên ngoài.',
        'q_08b923f3c5c5':'Nhà cung cấp chính xếp theo số phiếu nhập khác nhau, không theo tổng tiền; đã đọc sổ nhập gốc và gộp các cách viết tên.',
        'q_06de4545fe25':'Đã đọc cả hai file chi tiết hoá đơn; đếm số đơn hợp lệ khác nhau theo mã hàng, bỏ Phí phục vụ.',
    }
    details=[]
    for item in selection:
        identity=item['question']['id'];expected=raw[identity]['expected'];prediction=item['prediction']
        match=prediction.get('answer')==expected
        if isinstance(expected,str) and isinstance(prediction.get('answer'),str):
            match=key(expected)==key(prediction['answer'])
        verdict='abstain' if prediction['type']=='abstain' else 'match' if match else 'mismatch'
        details.append({**item,'independent_expected':expected,'audit':raw[identity],'verdict':verdict,
            'note':notes.get(identity,'Khớp phép tính độc lập trên dữ liệu gốc và rulebook; không có gold chính thức.')})
    counts=dict(Counter(r['verdict'] for r in details))
    assert counts=={'match':14,'mismatch':4,'abstain':2}
    bids={r['question']['business_id'] for r in selection}
    hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for bid in bids for p in (DATA/bid).iterdir() if p.is_file()}
    report={'counts':counts,'official_gold_available':False,'official_pa':None,
            'baseline':'public_50_trammay, stopped after 31 checkpoints before normalization fixes',
            'source_hashes':hashes,'details':details}
    (OUT/'review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    labels={'match':'Khớp','mismatch':'Sai','abstain':'Bỏ trả lời'}
    def cell(v):return str(v).replace('|','\\|').replace('\n',' ')
    def js(v):return json.dumps(v,ensure_ascii=False)
    lines=['# Đối chiếu 20 đáp án tiếp theo — câu 11 đến 30','',
        '**Kết quả: 14 khớp phép tính độc lập, 4 sai giá trị, 2 abstain.** Không có gold public; đây là kiểm tra tại máy, chưa phải kết quả từ bộ chấm chính thức.','',
        'Mẫu gồm 20 checkpoint hoàn thành tiếp theo sau nhóm 10 câu đầu, không phải câu 11–30 theo thứ tự questions.jsonl. Các prediction thuộc bản code/cache cũ; tiến trình cũ được dừng ở 31 checkpoint để thực hiện sửa chuẩn hoá.','',
        '- [20 prediction gốc JSONL](../result/public_test_next20.jsonl).',
        '- [Kết quả, SQL, số liệu đối chiếu và hash nguồn](../artifacts/public_next20/review.json).',
        '- [Script audit đọc nguồn gốc](../scripts/review_public_next20.py).','',
        '| # | ID | Câu hỏi nguyên văn | Model | Đối chiếu | Kết luận |',
        '|---|---|---|---|---|---|']
    for row in details:
        pred=row['prediction'];value=js(pred.get('answer')) if pred['type']!='abstain' else 'abstain'
        lines.append('| '+' | '.join(map(cell,[row['index'],row['question']['id'],row['question']['question'],value,js(row['independent_expected']),labels[row['verdict']]]))+' |')
    lines+=['','## Chi tiết và bằng chứng','']
    for row in details:
        q=row['question'];lines += [f"### {row['index']}. {q['id']}",'',q['question'],'',
            f"Doanh nghiệp `{q['business_id']}`, as_of_date `{q['as_of_date']}`.",'',
            f"**{labels[row['verdict']]}:** {row['note']}",'','Prediction gốc:','',
            '```json',js(row['prediction']),'```','',
            f"[Trace](../{row['trace']})."]
        if row['program']:
            lines+=['','```sql',row['program']['sql'],'```','',f"Parameters: `{js(row['program']['parameters'])}`."]
        lines.append('')
    lines+=['## Phạm vi kiểm tra','',
        'Đọc trực tiếp Excel/CSV/SQLite nguồn, dùng logic audit riêng cho câu hỏi, không dùng SQL model để tạo giá trị đối chiếu. Phép thử chỉ thay điều kiện nối tên của câu thuốc là bước tìm nguyên nhân sau khi đã tính trên nguồn. Tiền và phần trăm được làm tròn theo rulebook; đơn huỷ/trả bị loại cả đơn; danh sách giữ thứ tự; thợ/sổ không có mã phiếu giữ các lượt ghi như dữ liệu.', '',
        'Đối chiếu tên khách dùng các cách viết cùng xuất hiện trong một đơn để gộp identity; khách lẻ/kl bị loại. Việc này là audit có phạm vi, không phải bộ chấm tổng quát đã được xác thực. Các câu hoàn thành sớm không phải mẫu ngẫu nhiên.', '',
        'Gộp với 10 câu đầu: 20 câu khớp, 6 câu sai giá trị, 4 câu abstain trên 30 câu đã đối chiếu. Không suy ra PA chính thức hoặc độ chính xác toàn bộ public từ mẫu này.','']
    (ROOT/'docs/PUBLIC_TEST_NEXT20_REVIEW.md').write_text('\n'.join(lines),encoding='utf-8')
    print('REVIEW',counts,flush=True)


if __name__=='__main__':
    write_report() if '--report-only' in sys.argv else main()

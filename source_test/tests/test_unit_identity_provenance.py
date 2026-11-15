"""Unit metadata must travel with the actual identity conversion, not be guessed from numeric equality."""
from pathlib import Path
import json, sqlite3
from datetime import date
import pytest
from bizreason.contracts import BusinessContext, ColumnMeta, SourceRef, TableMeta, TimeWindow
from bizreason.semantic import layer
from bizreason.semantic.compiler import Compiler, Intent, Unsupported

def build(tmp_path, *, explicit=None, sold_unit='piece', factor=None, known_base=None):
    source=tmp_path/'input.sqlite'
    sales_cols=['order','day','item','unit','qty','supplied_base_qty','status','amount']
    sales=[('o1','2024-01-12','Loose item',sold_unit,'3',explicit,'completed','20'),
           ('o2','2024-01-12','Converted item','carton','2',None,'completed','40')]
    conv_cols=['item','large','base','factor']
    conversions=[('Converted item','carton','piece','12')]
    if factor is not None:
        conversions.append(('Loose item',sold_unit,None,str(factor)))
    if known_base is not None:
        conversions.append(('Loose item','documented unit',known_base,None))
    specs={'sales':(sales_cols,sales),'packing':(conv_cols,conversions)}
    db=sqlite3.connect(source)
    for name,(columns,rows) in specs.items():
        db.execute('CREATE TABLE '+name+' ('+','.join('"'+c+'" TEXT' for c in columns)+',__source_row INTEGER,__raw_json TEXT)')
        for index,row in enumerate(rows,1):
            db.execute('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in range(len(columns)+2))+')',(*row,index,'{}'))
    db.commit();db.close()
    context=BusinessContext(business_id='synthetic',revision='r',database=source,readme='',manifest={},tables=[
        TableMeta(name=name,sources=[SourceRef(path=name+'.csv')],columns=[ColumnMeta(name=c,original_name=c,dtype='text') for c in columns],
                  row_count=len(rows),raw_row_count=len(rows)) for name,(columns,rows) in specs.items()])
    records=[{'columns':sales_cols,'kind':'sales','roles':{'order':'order_id','day':'ordered_at','item':'product_name',
        'unit':'unit','qty':'qty','supplied_base_qty':'qty_base','status':'order_status','amount':'line_amount'}},
        {'columns':conv_cols,'kind':'unit_conversion','roles':{'item':'product_name','large':'large_unit','base':'base_unit','factor':'conversion_factor'}}]
    target=tmp_path/'canonical.sqlite'
    layer.build(context,records,target)
    return sqlite3.connect(target)

def test_identity_unit_kept_in_a_shop_with_converted_products(tmp_path):
    with build(tmp_path) as db:
        assert db.execute('SELECT unit,qty,qty_base,base_unit FROM lines WHERE product_name=?',['Loose item']).fetchone()==('piece',3,3,'piece')
        # A conversion elsewhere in the same shop must not hide the identity unit.
        answer=Compiler(db).answer(Intent(measure='qty_base_sold',filters=[{'dim':'unit','value':'piece'}]),
            [TimeWindow(role='target',start=date(2024,1,1),end_exclusive=date(2024,2,1))])
        assert answer.value==27

@pytest.mark.parametrize('explicit',['3','30'])
def test_direct_base_quantity_does_not_imply_its_unit_even_when_equal(tmp_path,explicit):
    with build(tmp_path,explicit=explicit) as db:
        assert db.execute('SELECT qty_base,base_unit FROM lines WHERE product_name=?',['Loose item']).fetchone()==(float(explicit),None)

@pytest.mark.parametrize('factor',[1,10])
def test_conversion_without_base_label_is_not_an_identity_unit(tmp_path,factor):
    with build(tmp_path,factor=factor) as db:
        assert db.execute('SELECT qty_base,base_unit FROM lines WHERE product_name=?',['Loose item']).fetchone()==(3*factor,None)

def test_known_base_with_missing_conversion_retains_unknown_quantity(tmp_path):
    with build(tmp_path,known_base='gram') as db:
        assert db.execute('SELECT qty_base,base_unit FROM lines WHERE product_name=?',['Loose item']).fetchone()==(None,'gram')

def test_missing_sold_unit_stays_unknown(tmp_path):
    with build(tmp_path,sold_unit=None) as db:
        assert db.execute('SELECT qty_base,base_unit FROM lines WHERE product_name=?',['Loose item']).fetchone()==(None,None)

def test_documented_conversion_is_unchanged(tmp_path):
    with build(tmp_path) as db:
        assert db.execute('SELECT qty,qty_base,base_unit FROM lines WHERE product_name=?',['Converted item']).fetchone()==(2,24,'piece')

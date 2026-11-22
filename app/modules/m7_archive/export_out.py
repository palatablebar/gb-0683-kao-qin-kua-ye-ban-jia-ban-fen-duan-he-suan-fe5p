import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    monthly=state.get('monthly')
    if monthly:
        writer.writerow(['月结',rid,'','',monthly.get('period',''),json.dumps({k:v for k,v in monthly.items() if k!='rows'},ensure_ascii=False,sort_keys=True)])
        for row in monthly.get('rows',[]):writer.writerow(['付款',rid,'',row['employee'],row['pay_type'],json.dumps(row,ensure_ascii=False,sort_keys=True)])
        payment=state.get('payment')
        if payment:writer.writerow(['付款登记',rid,'','',payment.get('period',''),json.dumps(payment,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '\ufeff'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()

import pandas as pd, numpy as np
st=pd.read_csv('nfl/data/raw/stats_player_week_2026.csv',low_memory=False)
st=st[st.season_type=='REG'].sort_values(['player_id','week'])
STATS={'ryd':'rushing_yards','recyd':'receiving_yards','rec':'receptions','car':'carries'}
rows=[]
for pid,g in st.groupby('player_id'):
    g=g.reset_index(drop=True)
    if g.position.iloc[-1] not in ('RB','WR','TE'): continue
    for lab,col in STATS.items():
        if lab=='car' and g.position.iloc[-1]!='RB': continue
        v=g[col].values; wk=g.week.values
        for i in range(5,len(g)):
            prev=v[:i]; avg=prev.mean(); l5=prev[-5:].mean()
            line=round(avg*2)/2
            if line<=0.5: continue
            rows.append((lab,int(wk[i]),l5/line-1,v[i]>line,v[i]<line,v[i]==line))
df=pd.DataFrame(rows,columns=['stat','week','dev','over','under','push'])
train=df[df.week<=8]; test=df[df.week>=9]
def eval_rule(d,stat,m,side):
    if side=='U': s=d[(d.stat==stat)&(d.dev<=-m)&(~d.push)]; hit=s.under
    else: s=d[(d.stat==stat)&(d.dev>=m)&(~d.push)]; hit=s.over
    return len(s), hit.mean() if len(s) else np.nan
out=[]
for stat in STATS:
  for side in ['U','O']:
    for m in [0,0.1,0.2,0.3]:
        n1,a1=eval_rule(train,stat,m,side); n2,a2=eval_rule(test,stat,m,side)
        out.append((stat,side,m,n1,round(a1*100,1) if n1 else None,n2,round(a2*100,1) if n2 else None))
r=pd.DataFrame(out,columns=['stat','lado','margen','n_train','acierto_train','n_test','acierto_test'])
pd.set_option('display.width',200)
print(r.to_string(index=False))

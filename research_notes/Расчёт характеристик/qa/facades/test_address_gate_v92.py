"""Regression tests for full suffix/corpus preservation, no network/data writes."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import facades as f
pairs=[('1','улица Бестужева, д.1 Б, Сочи',False),('1Б','улица Бестужева, д.1 Б, Сочи',True),('2','улица Карла Маркса, д.2 А, Сочи',False),('2А','улица Карла Маркса, д.2 А, Сочи',True),('13','улица Демьяна Бедного, д.13 А, Калининград',False),('13А','улица Демьяна Бедного, д.13 А, Калининград',True),('19/21','проспект Мира, зд.19/21, к.2',False),('19/21 к2','проспект Мира, зд.19/21, к.2',True),('19/21 к.2','проспект Мира, зд.19/21, корпус 2',True),('19/21 к2','проспект Мира, зд.19/21, корпус 3',False),('19','улица Луначарского, д.19, лит.А',False),('19А','улица Луначарского, д.19, лит.А',True),('26А','Улица Гаврилова 26 A, Казань',True),('73А','ulitsa Bagrationa, 73 A, Kaliningrad, Russia',True),('219/28','ул.Ленина, д.219/28',True),('219','ул.Ленина, д.219/28',False),('8','улица 8 Марта 13',True)]
# Last case tests lexical gate only: caller separately excludes street-number-only match.
checks=[]
for house,address,expected in pairs:
 result=f.full_house_gate(house,address);assert result==expected,(house,address,result,expected)
 checks.append({'house':house,'address':address,'expected':expected,'actual':result,'passed':True})
f.dump(f.BASE/'qa/facades/address_gate_v92_checks.json',{'passed':True,'n':len(checks),'version':f.FACADES_VERSION,'checks':checks})
print(len(checks),'address regression checks passed')

import re
from decimal import Decimal
from operator import eq, ge, gt, le, lt
from documents import proof
import inference

def currency(value):
    return f"{value:,.2f}".replace(","," ").replace(".",",").removesuffix(",00")+" ₽"

def contract_price(rows):
    price_pattern=r"(?:Стоимость поставки по данному договору составляет|(?:общая\s+)?(?:цена|стоимость)\s+(?:настоящего\s+)?договора\s*(?:составляет|равна|:|—))\s*([\d][\d ]*(?:[,.]\d{1,2})?)\s*(?:руб\.?|₽)"
    amounts={}
    for row in rows:
        for m in re.finditer(price_pattern,row["text"],re.I):
            value=Decimal(m[1].replace(" ","").replace(",","."))
            amounts[value]=proof(row,m.group())
    return next(iter(amounts.items())) if len(amounts)==1 else (None,None)

def verified(rows,items):
    index={r["id"]:r for r in rows}
    evidence=[]
    for e in items:
        if e["id"] not in index or len(e["quote"].strip())<8:
            raise ValueError("Ссылка на основание или позицию спецификации не подтверждена.")
        evidence.append(proof(index[e["id"]],e["quote"]))
    return evidence

def fact(value="", refs=()):
    return {"value": value, "refs": list(refs)}

def check(label, state, detail, policy=(), contract=()):
    return dict(label=label, state=state, detail=detail, policy=list(policy), contract=list(contract))

def route(card, state, detail, checks):
    return dict(category=card.get("category"), clause=card["clause"], title=card.get("title", card["clause"]), state=state,
                detail=detail, checks=checks, policy=card["refs"])

def conclude(facts, routes, blocker=None, complete=False):
    declared = facts["declared"]
    selected = None
    if declared.get("matched",True):
        selected = next((r for r in routes if r["clause"] == declared["value"]), None)
    alternatives = [r for r in routes if r is not selected and r["state"] in ("fit", "partial")]
    alternatives.sort(key=lambda r: r["state"] != "fit")
    if blocker:
        state, title, detail = "blocked", "Прямая закупка невозможна по этой редакции", blocker["detail"]
        alternatives = []
    elif selected:
        state = selected["state"]
        title = {"fit":"Соответствует указанному основанию", "partial":"Данных для подтверждения недостаточно",
                 "unknown":"Данных для подтверждения недостаточно", "excluded":"Не соответствует указанному основанию"}[state]
        detail = selected["detail"]
    elif declared.get("missing_clause"):
        state, title, detail = "excluded", "Указанный подпункт отсутствует в перечне", declared["note"]
    elif alternatives:
        state, title, detail = "suggest", "Есть возможное основание", "Первым показан вариант с наиболее полным подтверждением по документам."
    else:
        state, title, detail = "unknown", "Подходящее основание не установлено", "Представленных сведений недостаточно, чтобы рекомендовать прямую закупку."
    if complete and routes and all(r["state"] == "excluded" for r in routes):
        state, title, detail = "blocked", "Прямая закупка невозможна по этой редакции", "По каждому основанию установлено противоречие с документами."
    if selected and selected["state"] == "fit":
        alternatives = []
    return dict(facts=facts, state=state, title=title, detail=detail, selected=selected,
                alternatives=alternatives, routes=routes, blocker=blocker)

FACT_PROMPT = """Выдели из договора факты. Верни {"subject":{"value":"предмет с учётом спецификации","citations":[{"id":"D.1","quote":"цитата"}]},"price":{"value":"полная цена договора цифрами без пробелов","currency":"RUB","citations":[]},"declared":{"value":"точная ссылка на пункт положения или пустая строка","n":null,"note":"что нашлось в преамбуле/решении","citations":[]}}. n — индекс однозначно названного основания из списка, иначе null. Сохрани прямо указанную ссылку, даже когда её нет в каталоге. Ссылки на устав, сам договор или 223-ФЗ в целом не являются выбором основания. Не выводи выбор из предмета или суммы. Когда находишь только общую ссылку, приложи её цитату, value оставь пустым. Цитаты непрерывные и дословные. Если нет данных или цены противоречат друг другу, верни пустое value. Не переводи валюту. Для рублёвой цены укажи RUB, в иных случаях оставь цену пустой."""

CHECK_PROMPT = """Проверь КАЖДОЕ переданное основание ЕП в контексте общих условий, источника финансирования и поправок. Для каждого n верни {"routes":[{"n":0,"detail":"почему подходит, расходится или что запросить","checks":[{"label":"условие","state":"ok|risk|unknown","detail":"факт и вывод, либо какой именно документ нужен","citations":[{"id":"D.1","quote":"точная непрерывная цитата договора"}],"control_ids":[]}],"anchor":false,"complete":false}]}.
Проверь реальный предмет по строкам спецификации, цену, лимиты, специальные обстоятельства и все обязательные условия именно этого основания. Для предметного основания оцени каждую позицию спецификации, создай отдельную строку для каждой несовпадающей позиции. Условия альтернативных оснований не соединяй. Общий лимит по сумме допускает любой предмет только в указанных пределах. Не смешивай рубли с иной валютой. Для числового условия явно укажи обе величины в detail. Для risk нужно прямое доказанное противоречие; отсутствие документов означает unknown. Для ok и risk нужны цитаты договора. Если факт подвёл под конкретную норму из перечня общих условий или поправок, укажи её индекс в control_ids. Если редакция или контекст неоднозначны, дай unknown. anchor=true только если достаточная часть предмета/цены/события подтверждает реальную возможность именно этого варианта; для общего стоимостного основания обязательны предмет И укладывающаяся цена. Одно лишь отсутствие противоречия, стандартный форс-мажор и подпись ЭДО не дают оснований рекомендовать вариант. complete=true только если исчерпывающе проверены предмет, спецификация и все применимые условия основания. При отсутствии конкретной зацепки верни одно короткое unknown с тем, чего не хватает. Не придумывай обстоятельства."""


def model_decision(grounds, rows, controls, facts_only=False):
    if sum(len(r["text"]) for r in rows) > 115000:
        raise ValueError("Договор превышает 115 тыс. знаков.")
    model = inference.model_name()
    docs = inference.compact(rows)
    rules = [dict(n=i,clause=g["clause"],rule=g["rule"],source=g["refs"]) for i,g in enumerate(grounds)]
    common=[dict(n=i,clause=c["clause"],rule=c["rule"],source=c["refs"]) for i,c in enumerate(controls)]
    prompt = FACT_PROMPT + " Когда в договоре указана только поставка товара, установи группы товаров по полной спецификации и приложи отдельные дословные наименования для подтверждения."
    raw = inference.generate(prompt, dict(documents=docs, catalogue=[dict(n=r["n"],clause=r["clause"]) for r in rules]), model)
    facts = {}
    selected = None
    for name in ("subject","price","declared"):
        obj = raw[name]
        refs = verified(rows,obj["citations"])
        value = str(obj.get("value") or "").strip() if refs else ""
        if name == "price" and value:
            if obj.get("currency")=="RUB" and any(re.search(r"₽|руб|RUB",r["quote"],re.I) for r in refs):
                value = currency(inference.decimal_from_quote(value," ".join(r.get("excerpt",r["quote"]) for r in refs)))
            else:
                value = ""
        if name == "declared":
            quote = " ".join(r.get("excerpt",r["quote"]) for r in refs)
            idx = obj.get("n")
            # A quotation must support both the literal reference and its mapping to the catalogue.
            if value and value.casefold() not in quote.casefold():
                value = ""
            if value and type(idx) is int and 0 <= idx < len(grounds):
                numbers = re.findall(r"\d+(?:\.\d+)*",grounds[idx]["clause"])
                if numbers and all(re.search(r"(?<![\d.])"+re.escape(n)+r"(?!\d|\.\d)",value) for n in numbers):
                    selected = idx
                    value = grounds[idx]["clause"]
        facts[name] = fact(value,refs)
    facts["declared"]["note"] = str(raw["declared"].get("note") or "")
    fixed_price,price_evidence=contract_price(rows)
    if fixed_price is not None:
        if facts["price"]["value"] in {"",currency(fixed_price)}:
            facts["price"]=fact(currency(fixed_price),[price_evidence])
        else:
            facts["price"]=fact("",[price_evidence,*facts["price"]["refs"]])
    facts["declared"]["matched"] = selected is not None
    if facts_only:
        return facts
    if facts["declared"]["value"] and selected is None:
        facts["declared"]["note"] += " Связь ссылки с конкретной записью перечня не подтверждена."
    routes=[]
    for start in range(0,len(rules),6):
        target=rules[start:start+6]
        numeric_prompt=' Для сравнения числового условия укажи в проверке comparison={"actual":"число","limit":"число","op":"lt|le|eq|ge|gt"} для условия actual op limit. Сравнивай только в одинаковых единицах, с одной базой расчёта и с учётом включительности границ. Если единицы или предел неоднозначны, дай unknown.'
        result=inference.generate(CHECK_PROMPT+numeric_prompt+" Оценивай только признаки, следующие из текста конкретного переданного основания и его отсылок. Для специального основания в specific_citations дай ссылки {id,quote} на признак из договора; если он не найден, верни [].",dict(target_grounds=target,common_conditions=common,documents=docs),model)["routes"]
        if not isinstance(result,list) or sorted(x["n"] for x in result)!=[r["n"] for r in target] or any(type(x["n"]) is not int for x in result):
            raise ValueError("Модель пропустила или повторила основание. Повторите проверку.")
        for entry in sorted(result,key=lambda x:x["n"]):
            card=grounds[entry["n"]]
            checks=[]
            verified_price=False
            for x in entry["checks"]:
                state=x["state"]
                if state not in {"ok","risk","unknown"}:
                    raise ValueError("Неизвестный результат проверки основания.")
                ev=verified(rows,x["citations"])
                refs=list(card["refs"])
                for idx in x.get("control_ids",[]):
                    if type(idx) is not int or not 0 <= idx < len(controls):
                        raise ValueError("Не подтверждена ссылка на общее условие Положения.")
                    refs.extend(controls[idx]["refs"])
                if not ev and state!="unknown":
                    raise ValueError("Решающая проверка не подтверждена цитатой договора.")
                detail=x["detail"]
                cmp=x.get("comparison")
                if cmp and state in {"ok","risk"}:
                    try:
                        actual=inference.decimal_from_quote(cmp["actual"]," ".join(r.get("excerpt",r["quote"]) for r in ev))
                        limit=inference.decimal_from_quote(cmp["limit"]," ".join(r.get("excerpt",r["quote"]) for r in refs))
                        meets={"lt":lt,"le":le,"eq":eq,"ge":ge,"gt":gt}[cmp["op"]](actual,limit)
                        if meets!=(state=="ok"):
                            raise ValueError("Числовой вывод противоречит сравнению.")
                        if state=="ok" and currency(actual)==facts["price"]["value"]:
                            verified_price=True
                    except (ValueError,KeyError,TypeError):
                        state,detail="unknown","Сумму и границу условия не удалось подтвердить по цитатам. Сверьте единицы и формулировку порога."
                checks.append(check(x["label"],state,detail,refs,ev))
            states={x["state"] for x in checks}
            anchored = entry.get("anchor") is True and "ok" in states and bool(facts["subject"]["value"])
            specific = verified(rows, entry.get("specific_citations", [])) if card.get("category") != "Цена" else []
            if card.get("category") != "Цена":
                anchored = anchored and bool(specific)
            if card.get("category")=="Цена":
                anchored=anchored and verified_price
            if "risk" in states:
                state="excluded"
            elif anchored and states=={"ok"} and entry.get("complete") is True:
                state="fit"
            else:
                state="partial" if anchored else "unknown"
            detail=entry["detail"]
            if state=="unknown" and checks:
                detail=next((x["detail"] for x in checks if x["state"]=="unknown"),"Основание по представленным документам не подтверждено.")
            routes.append(dict(route(card,state,detail,checks),specificEvidence=specific))
    blocker=None
    decision=conclude(facts,routes,blocker,complete=False)
    if routes and all(r["state"]=="excluded" for r in routes):
        decision["title"]="Ни одно из извлечённых оснований не подходит"
        decision["detail"]="По каждому извлечённому пункту выявлено противоречие. Перед окончательным выводом нужна сверка полноты перечня с оригиналом."
    return decision


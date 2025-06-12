# ——— Выбор вкуса (Reply-клавиатура fallback) ———
cat0 = data.get('current_category')
if cat0:
    price = menu[cat0]["price"]
    for it in menu[cat0]["flavors"]:
        if it.get("stock", 0) > 0:
            emoji = it.get("emoji", "")
            flavor0 = it["flavor"]
            label = f"{emoji} {flavor0} — {price}₺ [{it['stock']} шт]"
            if text == label:
                data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price})
                template = t(chat_id, "added_to_cart")
                suffix = template.split("»", 1)[1].strip()
                count = len(data['cart'])
                bot.send_message(
                    chat_id,
                    f"«{cat0} — {flavor0}» {suffix.format(flavor=flavor0, count=count)}",
                    reply_markup=get_inline_main_menu(chat_id)
                )
                user_data[chat_id] = data
                return

    # если ввод не соответствует ни одному вкусу, показать только кнопку «Назад к категориям»
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        text=f"⬅️ {t(chat_id, 'back_to_categories')}",
        callback_data="go_back_to_categories"
    ))
    bot.send_message(
        chat_id,
        t(chat_id, "error_invalid"),
        reply_markup=kb
    )
    return

def handle(ctx):
    choice = ctx.get("steps", {}).get("cr_approval", {}).get("choice", "")

    if "通过" in choice:
        return {"status": "ok", "skip_goto": True}

    # 累加多轮意见
    prev = ctx.get("steps", {}).get("review_feedback", {}).get("output", "")
    if prev:
        rounds = prev.count("--- 第") + 1
    else:
        rounds = 0
    round_label = f"--- 第 {rounds + 1} 轮修改意见 ---"

    print()
    print("=" * 50)
    print(f"请输入修改意见（可以多行，输入 END 结束）：")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    new_feedback = "\n".join(lines)
    print("=" * 50)
    print(f"已记录修改意见 ({len(new_feedback)} 字符)")

    accumulated = f"{prev}\n\n{round_label}\n{new_feedback}" if prev else f"{round_label}\n{new_feedback}"
    return {"status": "ok", "output": accumulated}

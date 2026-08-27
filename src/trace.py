WIDTH = 68
MAX_LIST_ITEMS = 8
MAX_TEXT = 400


def _fmt(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + " ..."
    return text


def _render_list(key, items, indent):
    pad = "  " * indent
    if not items:
        return [f"{pad}{key}: (none)"]
    if all(isinstance(item, dict) for item in items):
        return [f"{pad}{key}: {len(items)} rows"]
    shown = ", ".join(_fmt(item) for item in items[:MAX_LIST_ITEMS])
    extra = len(items) - MAX_LIST_ITEMS
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return [f"{pad}{key}: {shown}{suffix}"]


def render(value, indent=0):
    pad = "  " * indent
    if not isinstance(value, dict):
        return [f"{pad}{_fmt(value)}"]
    lines = []
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(render(item, indent + 1))
        elif isinstance(item, list):
            lines.extend(_render_list(key, item, indent))
        else:
            lines.append(f"{pad}{key}: {_fmt(item)}")
    return lines


class Tracer:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def _out(self, text=""):
        if self.enabled:
            print(text)

    def question(self, text, model, endpoint):
        self._out("=" * WIDTH)
        self._out("USER")
        self._out(text)
        self._out("")
        self._out(f"model: {model}    endpoint: {endpoint}")
        self._out("=" * WIDTH)
        self._out()

    def llm_step(self, step, calls, content, elapsed):
        self._out(f"STEP {step} — LLM  ({elapsed:.1f}s)")
        if calls and content and content.strip():
            self._out(f"Says: {_fmt(content.strip())}")
        if not calls:
            self._out("Action: none, replying with a final answer")
        for call in calls:
            self._out(f"Action: {call['name']}")
            for key, value in call["arguments"].items():
                self._out(f"  {key}: {_fmt(value)}")
        self._out()

    def tool_result(self, step, name, result, elapsed):
        failed = isinstance(result, dict) and "error" in result
        label = "TOOL ERROR" if failed else "TOOL RESULT"
        self._out(f"STEP {step} — {label}  ({elapsed:.2f}s)")
        self._out(f"Tool: {name}")
        for line in render(result):
            self._out(line)
        self._out()

    def final(self, answer):
        self._out("FINAL ANSWER")
        self._out(answer.strip() if answer else "(the model returned no answer)")
        self._out()

    def stopped_early(self, max_iterations):
        self._out(f"STOPPED — hit the {max_iterations} step limit without a final answer")
        self._out()

    def summary(self, llm_calls, tool_calls, duration, log_path, transcript_path):
        self._out("-" * WIDTH)
        self._out(f"TOTAL LLM CALLS:      {llm_calls}")
        self._out(f"TOTAL GIS TOOL CALLS: {tool_calls}")
        self._out(f"TOTAL DURATION:       {duration:.1f}s")
        self._out(f"JSONL LOG:            {log_path}")
        self._out(f"TRANSCRIPT:           {transcript_path}")
        self._out("-" * WIDTH)
        self._out()

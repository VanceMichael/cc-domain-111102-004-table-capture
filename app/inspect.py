from pathlib import Path
from app.wire import Replay
from app.markup import tokens
print(len(tokens(Path("fixtures/trade.html").read_text())))

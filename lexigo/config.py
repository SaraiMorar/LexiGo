"""全局配置与常量。"""

from pathlib import Path

# 路径配置
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "lexigo.db"
WORDS_CSV_PATH = DATA_DIR / "words.csv"

# 条目类型
TYPE_WORD = "word"          # 单词（可带音标、词性）
TYPE_PHRASE = "phrase"      # 词组
TYPE_SENTENCE = "sentence"  # 句型

# 卡片方向
DIR_ZH2EN = "zh2en"  # 看中文想英文（中译英）
DIR_EN2ZH = "en2zh"  # 看英文想中文（英译中）

# 批次背诵顺序模式
ORDER_SEQUENCE = "sequence"  # 正序（按导入顺序）
ORDER_SHUFFLE = "shuffle"    # 随机乱序（每次进入重新随机）
ORDER_REVERSE = "reverse"    # 倒序
ORDER_MODES = (ORDER_SEQUENCE, ORDER_SHUFFLE, ORDER_REVERSE)

# 卡片状态
STATE_NEW = "new"        # 未学
STATE_LEARNING = "learning"  # 学习中（含当天重做）
STATE_REVIEW = "review"      # 复习中
STATE_MASTERED = "mastered"  # 已掌握（超长间隔）
STATE_SUSPENDED = "suspended"  # 手动搁置，不再复习，直到恢复

# 复习评级（SM-2）
RATING_AGAIN = 1  # 完全不会
RATING_HARD = 2   # 模糊
RATING_GOOD = 3   # 记住
RATING_EASY = 4   # 完全掌握

# SM-2 参数
MIN_EASE = 1.3
AGAIN_EASE_DELTA = -0.20
HARD_EASE_DELTA = -0.15
EASY_EASE_DELTA = 0.15
HARD_INTERVAL_FACTOR = 1.2
EASY_INTERVAL_FACTOR = 1.3

# Good 标准间隔序列（按成功次数 reps，从 1 开始）
GOOD_INTERVALS = [1, 3, 6, 15, 30, 60]
# 达到 mastered 状态的间隔阈值（天）
MASTER_INTERVAL_THRESHOLD = 90
# 新词首次 Easy 的间隔
FIRST_EASY_INTERVAL = 4

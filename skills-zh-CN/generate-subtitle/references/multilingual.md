# 多语言转写与翻译

## 语言与引擎

字幕默认使用音频原语言，与 SKILL.md、提示词、术语表、背景资料的语言无关。混合语言照原样保留。纠错不是翻译；只有用户明确要求翻译时才传 `--translate-to`。

- `--language auto`（默认）：逐文件、逐声道/设备轨用 Whisper 首/中/尾各最多 30 秒非静音采样检测；全部样本高置信度为 `zh` 才进入中文路径，否则用 Whisper。检测结果写入 manifest。抽样可能漏掉中途语言切换，不能宣称完整语言检测；发现漏识别时用 `--engine whisper`，已知音频语言时可给提示。
- `--language zh`：用户已知中文时直接使用中文路径，跳过语言检测。不要因为 Skill 文档是中文就设置它。
- 其他明确音频语言（如 `en`、`ja`、`ko`）：使用 Whisper；即便请求了中文引擎，也会提示改走 Whisper，避免中文模型强行转写。
- 中文路径沿用原规则：需声纹分离用 FunASR；否则优先 FireRed，缺失时退回 FunASR。显式 `--engine whisper` 也可处理中文或混合语言。
- `--whisper-model small`（默认）是多语言模型名，也可给本地 checkpoint 路径。不能用 `.en` 或任何英语单语 checkpoint。加载后根据实际模型的 `is_multilingual`、`num_languages` 及 tokenizer 语言 token 集合检查支持范围；不硬编码“所有 Whisper 模型都支持相同语言”。不支持的 `--language` 会报错，不静默换语言。
- Whisper 始终使用 `task="transcribe"` 和词时间戳；不会使用内置 `translate` 任务替代保留原文的转写步骤。混合语言识别质量仍取决于模型，纠错时不能用翻译掩盖识别问题。

## 依赖与设备

中文路径保留原有 FunASR / FireRed 环境隔离。自动语言检测和非中文转写需要在主脚本 Python 环境安装 **openai-whisper**（不是名为 `whisper` 的其他包）。字幕断句和字宽还需要 `regex`、`wcwidth`。不要合并已有 FunASR / FireRed venv 或为了本 Skill 改写其他工具环境。

```bash
# 使用实际运行脚本的解释器；先检查已安装依赖，缺失时再安装
python -m pip install openai-whisper regex wcwidth
```

`--device auto` 优先 CUDA、其次 MPS、最后 CPU。中文引擎保留已有设备逻辑；当前 Whisper 适配器使用 CPU 或 CUDA，词时间戳路径未验证 MPS，选择 MPS 时会明确提示并退回 CPU/FP32。CUDA 使用 FP16，CPU 使用 FP32。模型权重首次加载可能下载，体积依模型而异。

仅翻译已有字幕使用标准库和 handoff，不需要 ASR、torch、ffmpeg、regex 或 wcwidth。

## 可选翻译

先完成原语言字幕和纠错；有 `.uncertain.srt` / `.uncertain.vtt` 时先让人听原声确认。未确认前不能生成译文、花字或审查。**不要为了续跑自动清空存疑清单，也不要把时间流逝当成人工确认。** 人工改好原字幕后，把已处理的存疑清单归档或清空，再以该字幕为输入单独翻译，避免重跑音频覆盖人工修改。

```bash
# 原语言转写，纠错完成且无存疑后增加英文译文
python "$SKILL_DIR/generate_subtitle.py" talk.mp4 --language auto --translate-to en

# 已知日语音频，保留日文；翻译必须另行明确指定
python "$SKILL_DIR/generate_subtitle.py" talk.wav --language ja --whisper-model small

# 已有字幕单独翻译；不转写、不再次润色原文
python "$SKILL_DIR/generate_subtitle.py" talk.srt --translate-to en
# 或直接用只需标准库的入口
python "$SKILL_DIR/translate_subtitle.py" talk.srt --translate-to en
```

译文默认另存 `talk.en.srt` / `talk.ja.vtt`；`-o` 可指定同格式的新文件，不能覆盖源字幕。目标用明确的语言代码（如 `en`、`ja`、`zh-hant`），不能用 `auto`。目标语言不受 Whisper 支持列表限制，因为翻译由 handoff 完成。

翻译任务按内容与目标语言寻址，每批最多 100 条。exit 10 后按 `TASK.md` 写回 `out.json`，原命令续跑会复用已完成任务；更换目标或修改源字幕会重新生成受影响的任务。脚本保留原始条目编号、顺序、完整时间码行（含 VTT settings）、说话人标签、标签顺序及说话人换行；只替换文字。不对译文重切、合并、去重或改写时间轴，也不套用纠错的字数增长限制。译文变长时交给播放器显示，不为控制行宽破坏对应关系。

返回结构损伤时不写最终文件，原答案留为 `out.invalid.json`，原任务重新进入 handoff。翻译中发现原文含糊时返回 `uncertain`，脚本退出 2 并写 `talk.translation-en.uncertain.json`；人工确认后修正该任务答案、去掉已解决的 `uncertain` 字段再续跑。原字幕始终保留。

## 文字、分轨、时间戳

- Unicode `\X` 字素簇保证组合重音、Indic 连字和 ZWJ emoji 不被切开；`wcwidth` 按显示宽度计数，组合附加符号不重复占宽。字宽是终端列宽近似，不保证与视频字体像素完全一致。
- 有空格的文字按 Unicode 字母/数字及附加符号保全词，中文/日文等无空格文字按字素保底；泰文、缅文、高棉文同样保全字素，不宣称具备这些语言的词典断词。中文 jieba、量词和跨条接缝修复只用于明确中文。
- 非中文保留原标点、词间空格、重复和语气词。中文年份转换、去重、语气词清理、脏字替换及中英文空格只对明确中文条目执行；含日文假名/韩文时禁用中文自动改写。未知语言不猜中文，混合语言交模型保守判断。
- Whisper 词时间戳由秒换毫秒，按词文本顺次定位到原字符串，再映射到完整字素；无法对齐的局部只在相邻已知锚点内插值。原始 `words` 和公共 `timestamp` 同时保留，多设备偏移/漂移转换同时作用于二者。
- 当前 `paraformer-zh + fsmn-vad + ct-punc + cam++` 是已使用的中文声纹管线，不能由 cam++ 的存在推导出所有语言可用。非中文混音不能启用该声纹路径；Whisper 和 FireRed 适配器没有声纹分离。按声道/录音设备分轨不依赖语言，继续保留；一条物理轨里有多人时仍不能靠轨号区分。
- 语言检测缓存包含输入文件/轨道处理方案、Whisper 模型身份；ASR 缓存进一步区分实际引擎、音频提示、检测语言、模型、声纹人数与门限/对齐方案。本地 checkpoint 身份包含路径、大小、纳秒修改时间。旧版缓存不复用；仅改变翻译目标不使 ASR 缓存失效。

## 验证

```bash
python -m unittest discover -s "$SKILL_DIR/tests" -v
```

自动测试覆盖语言路由/模型语言集合、CPU/CUDA/MPS 选择、Unicode 原文与字素边界、词时间戳及设备偏移、中文规则隔离、翻译 handoff 续跑/存疑阻断/编号时间码标记完整性。ASR 模型测试用替身验证调用协议，不代表真实录音识别质量；新增模型或设备后仍应以代表性录音试听抽检。

Whisper 实现依据：[转写与词时间戳](https://github.com/openai/whisper/blob/main/whisper/transcribe.py)、[模型属性](https://github.com/openai/whisper/blob/main/whisper/model.py)、[tokenizer 语言集合](https://github.com/openai/whisper/blob/main/whisper/tokenizer.py)。

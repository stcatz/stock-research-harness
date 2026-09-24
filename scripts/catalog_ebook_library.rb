#!/usr/bin/env ruby
# frozen_string_literal: true

require "csv"
require "digest"
require "fileutils"
require "time"

SOURCE_ROOT = File.expand_path(ARGV[0] || "2600")
OUTPUT_ROOT = File.expand_path(ARGV[1] || File.join(SOURCE_ROOT, "_catalog"))
OUTPUT_NAME = File.basename(OUTPUT_ROOT)

unless Dir.exist?(SOURCE_ROOT)
  warn "Source directory does not exist: #{SOURCE_ROOT}"
  exit 1
end

AD_PATTERN = /免费零资料|公众号|更多资料|客服|风险提醒|加微信|\Aweixin(?:【|\.|\z)/i

CATEGORY_RULES = [
  ["05 衍生品与多资产", "期权/权证/衍生品", /期权|权证|衍生品|波动率|希腊字母/],
  ["05 衍生品与多资产", "期货/外汇/黄金/商品", /期货|商品交易|大宗商品|外汇|黄金|白银|原油|股指|汇率|贵金属/],
  ["04 基金债券与个人理财", "基金/债券/资产配置", /基金|ETF|定投|债券|可转债|保险|理财|资产配置|财富管理|家庭财富|养老金|养老金融|信托|私募|对冲基金/],
  ["03 基本面与价值投资", "价值/成长/估值/财报", /巴菲特|格雷厄姆|聪明的投资者|价值投资|成长股|估值|基本面|财报|财务报表|年报|彼得.?林奇|费雪|护城河|安全边际|证券分析|价值评估|公司分析|行业研究/],
  ["02 交易系统与投资理念", "系统/心理/风控", /交易心理|证券心理|行为金融|投资心理|交易系统|资金管理|仓位|止损|风险控制|风险管理|海龟交易|趋势跟踪|以交易为生|交易法则|交易策略|交易哲学|投机原理|投资理念|投资哲学|投资思维|投资原则|投资策略|交易思维|非赌博式交易|克罗谈投资策略|投资中最简单的事/],
  ["06 投资人物与市场纪实", "人物/经典/纪实", /利弗莫尔|索罗斯|华尔街|投机之王|大投机家|市场巫师|投资大师|金融大鳄|操盘手记|股神|投资家|投资传奇|回忆录|股票大作手|作手回忆录|滚雪球|战胜华尔街|说谎者的扑克牌|交易冠军|顶尖交易员|十年一梦/],
  ["01 股票技术与短线", "超短/涨停/盘口/选股", /淘股吧|塞外书生|不死鸟韦一|乱世浮生|字母哥|林疯狂|一击超人|深山老牛|涨停|打板|龙头|低吸|半路|反包|连板|二板|首板|超短|短线|盘口|分时|竞价|题材|黑马|庄家|看盘|量柱|量学|筹码|主升浪|强势股|涨停板|抢反弹|逃顶|抄底|解套|战法|买卖点|牛股|实盘|复盘|大盘|收评|交割单|持仓|擒庄|主力/],
  ["01 股票技术与短线", "K线/量价/趋势/指标", /K线|Ｋ线|k线|蜡烛图|均线|量价|成交量|波浪|江恩|布林|MACD|技术分析|技术指标|图表|形态|趋势线|道氏|切线|点位|循环周期|股价波动|股市趋势|价格形态|缠论/],
  ["01 股票技术与短线", "股票综合/入门/实战", /炒股|股市|股票|操盘|股民|证券投资|选股|A股|港股|新三板|创业板|科创板|牛市|熊市/],
  ["09 会计审计法律与教材", "会计/财务/审计/税务", /会计|审计|税务|税法|财务管理|公司金融|企业融资|融资管理|成本管理|财务分析|资产评估/],
  ["09 会计审计法律与教材", "法律/法规", /法律|法学|法规|经济法|证券法|公司法|金融法|监管法|知识产权/],
  ["09 会计审计法律与教材", "学术/教材/研究方法", /统计学|数学|计量|教材|教程|概论|实验|实训|教学|研究方法|博弈论|运筹学|时间序列|多重分形|复杂特性|实证研究|模型研究/],
  ["06 投资人物与市场纪实", "人物/经典/纪实", /传记|名人|商界名人|金融史|危机史|投行|富可敌国|金钱游戏/],
  ["08 公司治理与商业管理", "公司治理/并购/资本运作", /公司治理|股权|董事会|上市公司|并购|兼并|收购|公司价值|资本运作|融资租赁/],
  ["08 公司治理与商业管理", "创业/商业/管理", /企业|公司|创业|商业|营销|销售|管理|领导|品牌|团队|职场|商人|商道|经营|商业模式|互联网\+|互联网＋|创新|组织|人力资源|供应链|零售|电商/],
  ["07 宏观经济金融与政策", "宏观/金融/政策/资本市场", /宏观|经济|金融|货币|银行|资本市场|金融市场|全球化|国际贸易|财政|人民币|供给侧|产业政策|区域发展|城市发展|农村金融|普惠金融|互联网金融|金融科技|自贸区|一带一路|国家战略|改革|转型|发展报告|产业发展|中国.*投资|国际投资|对外投资|外商投资|投融资|利率市场化|证券市场/],
  ["10 投资综合与待细分", "综合投资/交易", /投资|交易|投机|证券|财富|赚钱|财商|财经|收益|套利|盈利|资本/]
].freeze

def relative_path(path)
  path.sub(/\A#{Regexp.escape(SOURCE_ROOT + File::SEPARATOR)}/, "")
end

def clean_title(name)
  title = name.dup
  title.sub!(/\.(pdf|epub|mobi|azw3|docx?|txt|jpe?g|png|mp4|rar|zip|7z|exe|pps|sel)\z/i, "")
  title.gsub!(/【薇信\d+代找电子书】/, "")
  title.gsub!(/【客服微[^】]*】/, "")
  title.gsub!(/【更多资料[^】]*】/, "")
  title.gsub!(/\((高清|高彩|清晰|完整版|最新版|珍藏版|扫描版)\)/i, "")
  title.gsub!(/(?:_|\s)20\d{12,14}\z/, "")
  title.gsub!(/\s+/, " ")
  title.strip!
  title
end

def extension_for(path)
  ext = File.extname(path).sub(/\A\./, "").downcase
  ext.empty? ? "[无扩展名]" : ext
end

def role_for(path)
  basename = File.basename(path)
  ext = extension_for(path)
  return "系统文件" if basename == ".DS_Store"
  return "推广或风险提示附件" if basename.match?(AD_PATTERN)
  return "电子书" if %w[pdf epub mobi azw3].include?(ext)
  return "普通文档" if %w[doc docx txt].include?(ext)
  return "视频" if %w[mp4 mov mkv avi].include?(ext)
  return "可执行程序" if %w[exe app dmg pkg].include?(ext)
  return "压缩包" if %w[rar zip 7z tar gz].include?(ext)
  return "图片/演示/数据" if %w[jpg jpeg png gif pps ppt pptx sel].include?(ext)

  "其他文件"
end

def classify_text(text)
  CATEGORY_RULES.each do |category, subcategory, pattern|
    return [category, subcategory, "high", pattern.source] if text.match?(pattern)
  end
  ["11 跨领域及非投资", "其他/待人工复核", "low", ""]
end

def classify_file(path, inherited)
  role = role_for(path)
  return ["98 非书目附件", role, "high", AD_PATTERN.source] if role == "推广或风险提示附件" || role == "系统文件"
  return ["99 软件压缩包与待检查", role, "high", extension_for(path)] if %w[可执行程序 压缩包].include?(role)

  own = classify_text(clean_title(File.basename(path)))
  return own unless own[0] == "11 跨领域及非投资" && inherited

  [inherited[0], inherited[1], "inherited", "顶层目录分类"]
end

def direct_top_entries
  Dir.children(SOURCE_ROOT)
     .reject { |name| name == OUTPUT_NAME }
     .sort
end

def files_under(path)
  return [path] if File.file?(path)
  return [] unless Dir.exist?(path)

  Dir.glob(File.join(path, "**", "*"), File::FNM_DOTMATCH).select { |candidate| File.file?(candidate) }
end

FileUtils.mkdir_p(OUTPUT_ROOT)

top_rows = []
file_rows = []
all_files = []

direct_top_entries.each do |name|
  path = File.join(SOURCE_ROOT, name)
  files = files_under(path)
  all_files.concat(files)
  roles = files.group_by { |file| role_for(file) }.transform_values(&:length)
  formats = files.group_by { |file| extension_for(file) }.transform_values(&:length)
  context = ([name] + files.reject { |file| role_for(file) == "推广或风险提示附件" }
                           .map { |file| clean_title(File.basename(file)) })
            .join(" ")

  if File.file?(path) && %w[推广或风险提示附件 系统文件].include?(role_for(path))
    category = "98 非书目附件"
    subcategory = role_for(path)
    confidence = "high"
    trigger = AD_PATTERN.source
  elsif File.file?(path) && %w[可执行程序 压缩包].include?(role_for(path))
    category = "99 软件压缩包与待检查"
    subcategory = role_for(path)
    confidence = "high"
    trigger = extension_for(path)
  else
    category, subcategory, confidence, trigger = classify_text(context)
  end

  top_rows << {
    "clean_title" => clean_title(name),
    "entry_type" => File.directory?(path) ? "collection_directory" : "file",
    "category" => category,
    "subcategory" => subcategory,
    "confidence" => confidence,
    "file_count" => files.length,
    "size_bytes" => files.sum { |file| File.size(file) },
    "formats" => formats.sort.map { |ext, count| "#{ext}:#{count}" }.join(";"),
    "roles" => roles.sort.map { |role, count| "#{role}:#{count}" }.join(";"),
    "relative_path" => name,
    "matched_rule" => trigger
  }

  files.each do |file|
    file_category, file_subcategory, file_confidence, file_trigger = classify_file(file, [category, subcategory])
    file_rows << {
      "clean_title" => clean_title(File.basename(file)),
      "category" => file_category,
      "subcategory" => file_subcategory,
      "confidence" => file_confidence,
      "role" => role_for(file),
      "extension" => extension_for(file),
      "size_bytes" => File.size(file),
      "modified_at" => File.mtime(file).iso8601,
      "top_level_entry" => name,
      "relative_path" => relative_path(file),
      "matched_rule" => file_trigger
    }
  end
end

top_headers = %w[clean_title entry_type category subcategory confidence file_count size_bytes formats roles relative_path matched_rule]
file_headers = %w[clean_title category subcategory confidence role extension size_bytes modified_at top_level_entry relative_path matched_rule]

CSV.open(File.join(OUTPUT_ROOT, "top_level_catalog.csv"), "wb", write_headers: true, headers: top_headers) do |csv|
  top_rows.each { |row| csv << row.values_at(*top_headers) }
end

CSV.open(File.join(OUTPUT_ROOT, "file_inventory.csv"), "wb", write_headers: true, headers: file_headers) do |csv|
  file_rows.each { |row| csv << row.values_at(*file_headers) }
end

review_rows = top_rows.select { |row| row["confidence"] == "low" }
CSV.open(File.join(OUTPUT_ROOT, "review_queue.csv"), "wb", write_headers: true, headers: top_headers) do |csv|
  review_rows.each { |row| csv << row.values_at(*top_headers) }
end

size_groups = all_files.group_by { |file| File.size(file) }
duplicate_groups = []
size_groups.each do |size, paths|
  next if size.zero? || paths.length < 2

  paths.group_by { |path| Digest::SHA256.file(path).hexdigest }.each do |sha256, matches|
    duplicate_groups << [sha256, size, matches] if matches.length > 1
  end
end
duplicate_groups.sort_by! { |_sha256, size, paths| -(size * (paths.length - 1)) }

duplicate_headers = %w[group_id sha256 size_bytes role relative_path]
CSV.open(File.join(OUTPUT_ROOT, "exact_duplicates.csv"), "wb", write_headers: true, headers: duplicate_headers) do |csv|
  duplicate_groups.each_with_index do |(sha256, size, paths), index|
    group_id = format("D%04d", index + 1)
    paths.sort.each do |path|
      csv << [group_id, sha256, size, role_for(path), relative_path(path)]
    end
  end
end

summary = {
  "generated_at" => Time.now.iso8601,
  "source_root" => SOURCE_ROOT,
  "top_level_entries" => top_rows.length,
  "files" => all_files.length,
  "size_bytes" => all_files.sum { |file| File.size(file) },
  "latest_input_mtime" => all_files.map { |file| File.mtime(file) }.max&.iso8601,
  "exact_duplicate_groups" => duplicate_groups.length,
  "duplicate_files_beyond_first" => duplicate_groups.sum { |_sha256, _size, paths| paths.length - 1 },
  "recoverable_duplicate_bytes" => duplicate_groups.sum { |_sha256, size, paths| size * (paths.length - 1) },
  "low_confidence_top_entries" => review_rows.length
}

CSV.open(File.join(OUTPUT_ROOT, "summary.csv"), "wb") do |csv|
  summary.each { |key, value| csv << [key, value] }
end

puts summary.map { |key, value| "#{key}=#{value}" }

"""Every sentence a caller can hear, in every language this line speaks.

WHY THE STRINGS LEFT reply_templates.py
---------------------------------------
reply_templates.py's job is DECIDING what to say: which branch of a tool
result the caller is in, whether a written confirmation may be promised,
which of three "sorry" answers is the honest one. That logic is the same
in every language. Interleaving three translations of every sentence into
those branches would triple the file and hide the decisions inside the
prose.

So the decisions stay there and the words live here. reply_templates.py
keeps its exact structure and its exact Bengali output; what changed is
that a literal became a lookup.

THE NO-SMARTPHONE RULE IS ENFORCED HERE
---------------------------------------
The story this file serves is "every flow completes without a smartphone".
That is ultimately a claim about TEXT: a flow dead-ends on a smartphone
the moment a sentence in this table tells a caller to tap a link, scan a
code, open an app or visit a website.

Nothing in this file may contain a URL, a link, a QR instruction or an app
name, and tests/test_no_smartphone.py asserts that over every string here
-- including the ones added later by somebody who has not read this
docstring. That test is the actual guardrail; this paragraph is only the
reason it exists.

Where a flow genuinely needs something we cannot do over a phone line, the
answer is the COUNTER, stated as a first-class completion path rather than
as an apology. See the `payment.*` and `report.*` keys.

TRANSLATION STATUS -- READ BEFORE SHIPPING
------------------------------------------
The Bengali strings are the originals, moved here unchanged. The Hindi and
English are mine and have NOT been reviewed by a speaker of either. They
are structurally correct and safe to test with; they are not yet safe to
put in front of patients. Flagged in the implementation notes as
outstanding rather than left to be discovered by a caller.
"""
from __future__ import annotations

from agent import language as lang_mod

# ---------------------------------------------------------------------------
# The table. One key per sentence; one entry per language.
#
# Keys are namespaced by flow so a missing translation is obvious at a
# glance and so the no-smartphone test can report WHICH flow broke the rule.
# ---------------------------------------------------------------------------
_STRINGS: dict[str, dict[str, str]] = {

    # -- generic ------------------------------------------------------------
    "ask.fallback": {
        "bn": "দুঃখিত, একটু স্পষ্ট করে বলবেন?",
        "hi": "माफ़ कीजिए, थोड़ा और साफ़ बताएँगे?",
        "en": "Sorry, could you say that a little more clearly?",
    },
    "generic.counter": {
        "bn": "আমাদের কাউন্টারে খোঁজ নিতে পারেন।",
        "hi": "आप हमारे काउंटर पर पूछ सकते हैं।",
        "en": "You can ask at our counter.",
    },
    "generic.tool_failure": {
        "bn": "এই মুহূর্তে দেখতে পারছি না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
        "hi": "इस वक़्त देख नहीं पा रहा हूँ। कृपया काउंटर पर संपर्क करें।",
        "en": "I cannot check that right now. Please contact the counter.",
    },

    "fallback.greeting": {
        "bn": "নমস্কার, কী সাহায্য করতে পারি?",
        "hi": "नमस्ते, मैं आपकी क्या मदद कर सकता हूँ?",
        "en": "Hello, how can I help you?",
    },
    "fallback.unclear": {
        "bn": "দুঃখিত, বুঝতে পারিনি। আবার একটু বলবেন?",
        "hi": "माफ़ कीजिए, समझ नहीं पाया। ज़रा फिर से बताएँगे?",
        "en": "Sorry, I did not catch that. Could you say it again?",
    },
    "fallback.llm_failure": {
        "bn": "একটু সমস্যা হচ্ছে, একটু ধরুন।",
        "hi": "थोड़ी दिक़्क़त हो रही है, ज़रा रुकिए।",
        "en": "I am having a little trouble, one moment please.",
    },

    # -- missing slot prompts ------------------------------------------------
    "ask.test_rate.test_name": {
        "bn": "কোন টেস্টের রেট জানতে চান, একটু বলবেন?",
        "hi": "किस टेस्ट का रेट जानना चाहते हैं, बताएँगे?",
        "en": "Which test's rate would you like to know?",
    },
    "ask.doctor_availability.doctor_name": {
        "bn": "কোন ডাক্তারের কথা জিজ্ঞেস করছেন?",
        "hi": "आप किस डॉक्टर के बारे में पूछ रहे हैं?",
        "en": "Which doctor are you asking about?",
    },
    "ask.doctors_by_department.department": {
        "bn": "কোন বিভাগের ডাক্তার খুঁজছেন?",
        "hi": "किस विभाग के डॉक्टर को खोज रहे हैं?",
        "en": "Which department's doctor are you looking for?",
    },
    "ask.doctors_by_department.date": {
        "bn": "কোন দিনের জন্য জানতে চান, একটু বলবেন?",
        "hi": "किस दिन के लिए जानना चाहते हैं?",
        "en": "Which day would you like to know about?",
    },
    "ask.book_appointment.doctor_name": {
        "bn": "কোন ডাক্তারের সাথে অ্যাপয়েন্টমেন্ট করতে চান?",
        "hi": "किस डॉक्टर के साथ अपॉइंटमेंट चाहिए?",
        "en": "Which doctor would you like the appointment with?",
    },
    "ask.book_appointment.date": {
        "bn": "আজকের জন্য চান, নাকি অন্য কোনো দিনের জন্য অ্যাপয়েন্টমেন্ট চাই?",
        "hi": "आज के लिए चाहिए, या किसी और दिन के लिए?",
        "en": "Would you like it for today, or for another day?",
    },
    "ask.book_appointment.time_slot": {
        "bn": "কোন সময়ে অ্যাপয়েন্টমেন্ট চাই, একটু বলবেন?",
        "hi": "किस समय का अपॉइंटमेंट चाहिए?",
        "en": "What time would you like the appointment?",
    },
    "ask.book_appointment.patient_name": {
        "bn": "রোগীর নামটা বলবেন?",
        "hi": "मरीज़ का नाम बताएँगे?",
        "en": "Could you tell me the patient's name?",
    },
    "ask.book_appointment.phone": {
        "bn": "একটা ফোন নম্বর দেবেন, যাতে কনফার্মেশন পাঠাতে পারি?",
        "hi": "एक फ़ोन नंबर देंगे, ताकि कन्फर्मेशन भेज सकूँ?",
        "en": "Could you give me a phone number, so I can send the confirmation?",
    },

    # -- test rates ----------------------------------------------------------
    "test.not_found_suggest": {
        "bn": "'{query}' নামে টেস্ট খুঁজে পাইনি। আপনি কি বলতে চাইছেন: {suggestions}?",
        "hi": "'{query}' नाम का टेस्ट नहीं मिला। क्या आपका मतलब है: {suggestions}?",
        "en": "I could not find a test called '{query}'. Did you mean: {suggestions}?",
    },
    "test.not_found": {
        "bn": "দুঃখিত, '{query}' নামে কোনো টেস্ট আমাদের তালিকায় নেই।",
        "hi": "माफ़ कीजिए, '{query}' नाम का कोई टेस्ट हमारी सूची में नहीं है।",
        "en": "Sorry, there is no test called '{query}' in our list.",
    },
    "test.rate": {
        "bn": "{name} টেস্টের রেট {rate} টাকা।",
        "hi": "{name} टेस्ट का रेट {rate} रुपये है।",
        "en": "The {name} test costs {rate} rupees.",
    },
    "test.rate_bare": {
        "bn": "{name} রেট {rate} টাকা।",
        "hi": "{name} का रेट {rate} रुपये है।",
        "en": "{name} costs {rate} rupees.",
    },
    "test.sample": {
        "bn": " স্যাম্পল: {sample}।",
        "hi": " सैंपल: {sample}।",
        "en": " Sample: {sample}.",
    },
    "test.report_hours": {
        "bn": " রিপোর্ট {hours} ঘণ্টার মধ্যে পাবেন।",
        "hi": " रिपोर्ट {hours} घंटे में मिल जाएगी।",
        "en": " The report will be ready within {hours} hours.",
    },

    # -- doctor availability -------------------------------------------------
    "doctor.not_found": {
        "bn": "দুঃখিত, '{query}' নামে কোনো ডাক্তার আমাদের এখানে নেই।",
        "hi": "माफ़ कीजिए, '{query}' नाम के कोई डॉक्टर यहाँ नहीं हैं।",
        "en": "Sorry, there is no doctor called '{query}' here.",
    },
    "doctor.available": {
        "bn": "হ্যাঁ,{date_txt} {name} চেম্বারে থাকবেন। সময়: {hours}। "
              "আজকের জন্যই অ্যাপয়েন্টমেন্ট করবেন, নাকি অন্য কোনো দিনের জন্য?",
        "hi": "जी हाँ,{date_txt} {name} चेंबर में रहेंगे। समय: {hours}। "
              "आज के लिए अपॉइंटमेंट करें, या किसी और दिन के लिए?",
        "en": "Yes,{date_txt} {name} will be in chamber. Hours: {hours}. "
              "Shall I book for today, or for another day?",
    },
    "doctor.date_on": {
        "bn": " {date} তারিখে",
        "hi": " {date} को",
        "en": " on {date}",
    },
    "doctor.date_today": {
        "bn": " আজ",
        "hi": " आज",
        "en": " today",
    },
    "doctor.next_date": {
        "bn": "{name} ওই দিন বসবেন না। পরবর্তী উপলব্ধ দিন: {next_date}। "
              "ওই দিনের জন্য অ্যাপয়েন্টমেন্ট করতে চান?",
        "hi": "{name} उस दिन नहीं बैठेंगे। अगला उपलब्ध दिन: {next_date}। "
              "उस दिन के लिए अपॉइंटमेंट चाहिए?",
        "en": "{name} does not sit that day. Next available day: {next_date}. "
              "Would you like an appointment then?",
    },
    "doctor.no_days": {
        "bn": "{name} এখন কোনো নির্দিষ্ট দিন বসছেন না। আমাদের কাউন্টারে খোঁজ নিতে পারেন।",
        "hi": "{name} अभी किसी निश्चित दिन नहीं बैठ रहे। आप हमारे काउंटर पर पूछ सकते हैं।",
        "en": "{name} does not currently sit on any fixed day. You can ask at our counter.",
    },

    # -- departments ---------------------------------------------------------
    "department.not_found": {
        "bn": "দুঃখিত, '{query}' নামে কোনো বিভাগ আমাদের এখানে নেই।",
        "hi": "माफ़ कीजिए, '{query}' नाम का कोई विभाग यहाँ नहीं है।",
        "en": "Sorry, there is no department called '{query}' here.",
    },

    "department.none_today": {
        "bn": "দুঃখিত, {department} বিভাগে আজ কোনো ডাক্তার নেই। অন্য কোনো দিনের কথা জিজ্ঞেস করতে পারেন।",
        "hi": "माफ़ कीजिए, {department} विभाग में आज कोई डॉक्टर नहीं है। किसी और दिन के बारे में पूछ सकते हैं।",
        "en": "Sorry, there is no doctor in {department} today. You could ask about another day.",
    },
    "department.none": {
        "bn": "{department} বিভাগে কোনো ডাক্তার নেই।",
        "hi": "{department} विभाग में कोई डॉक्टर नहीं है।",
        "en": "There is no doctor in {department}.",
    },
    "department.listing": {
        "bn": "{department} বিভাগে {names} আছেন।",
        "hi": "{department} विभाग में {names} हैं।",
        "en": "In {department} we have {names}.",
    },
    "department.ask_which": {
        "bn": " অ্যাপয়েন্টমেন্টের জন্য কোন ডাক্তারের নাম বলবেন?",
        "hi": " अपॉइंटमेंट के लिए किस डॉक्टर का नाम बताएँगे?",
        "en": " Which doctor would you like the appointment with?",
    },
    "conj.and": {"bn": " এবং ", "hi": " और ", "en": " and "},
    "honorific.doctor": {"bn": "ডাঃ {name}", "hi": "डॉ. {name}", "en": "Dr. {name}"},
    "word.doctor": {"bn": "ডাক্তার", "hi": "डॉक्टर", "en": "the doctor"},
    "word.test": {"bn": "টেস্ট", "hi": "टेस्ट", "en": "test"},

    # -- booking -------------------------------------------------------------
    "booking.success": {
        "bn": "আপনার অ্যাপয়েন্টমেন্ট কনফার্ম হয়েছে। {doctor}, {date}, সময় {time}। "
              "কনফার্মেশন নম্বর: {cid}।",
        "hi": "आपका अपॉइंटमेंट कन्फर्म हो गया है। {doctor}, {date}, समय {time}। "
              "कन्फर्मेशन नंबर: {cid}।",
        "en": "Your appointment is confirmed. {doctor}, {date}, at {time}. "
              "Confirmation number: {cid}.",
    },
    "booking.written_clause": {
        "bn": " কনফার্মেশনের একটা মেসেজ আপনার ফোনে পাঠানো হচ্ছে, রিসেপশনে ওটা দেখালেই হবে।",
        "hi": " कन्फर्मेशन का एक मैसेज आपके फ़ोन पर भेजा जा रहा है, रिसेप्शन पर वही दिखा दीजिए।",
        "en": " A confirmation message is being sent to your phone; just show it at reception.",
    },
    "booking.written_fallback": {
        "bn": " নম্বরটা মনে রাখতে না পারলেও চিন্তা নেই — রিসেপশনে আপনার নাম আর ফোন নম্বর বললেই "
              "ওঁরা অ্যাপয়েন্টমেন্ট খুঁজে দেবেন।",
        "hi": " नंबर याद न रहे तो भी चिंता नहीं — रिसेप्शन पर अपना नाम और फ़ोन नंबर बता दीजिए, "
              "वे अपॉइंटमेंट ढूँढ देंगे।",
        "en": " If you cannot remember the number, do not worry — give your name and phone "
              "number at reception and they will find the appointment.",
    },
    "booking.slot_taken_alts": {
        "bn": "ওই সময়টা বুক হয়ে গেছে। এই সময়গুলো ফাঁকা আছে: {alts}। কোনটা চান?",
        "hi": "वह समय बुक हो चुका है। ये समय खाली हैं: {alts}। कौन सा चाहिए?",
        "en": "That time is already booked. These are free: {alts}. Which would you like?",
    },
    "booking.slot_taken_none": {
        "bn": "ওই সময়টা বুক হয়ে গেছে, এবং কাছাকাছি কোনো সময় ফাঁকা নেই।",
        "hi": "वह समय बुक हो चुका है, और आसपास कोई समय खाली नहीं है।",
        "en": "That time is booked, and there is nothing free nearby.",
    },
    "booking.failed": {
        "bn": "দুঃখিত, অ্যাপয়েন্টমেন্ট বুক করা গেল না। একটু পরে আবার চেষ্টা করুন, "
              "অথবা কাউন্টারে যোগাযোগ করুন।",
        "hi": "माफ़ कीजिए, अपॉइंटमेंट बुक नहीं हो सका। थोड़ी देर बाद फिर कोशिश करें, "
              "या काउंटर पर संपर्क करें।",
        "en": "Sorry, the appointment could not be booked. Please try again shortly, "
              "or contact the counter.",
    },

    # -- reschedule / cancel -------------------------------------------------
    "reschedule.success": {
        "bn": "আপনার অ্যাপয়েন্টমেন্ট বদলে দেওয়া হয়েছে। {doctor}, {date}, সময় {time}। "
              "কনফার্মেশন নম্বর একই থাকছে: {cid}।",
        "hi": "आपका अपॉइंटमेंट बदल दिया गया है। {doctor}, {date}, समय {time}। "
              "कन्फर्मेशन नंबर वही रहेगा: {cid}।",
        "en": "Your appointment has been moved. {doctor}, {date}, at {time}. "
              "The confirmation number stays the same: {cid}.",
    },
    "reschedule.not_found": {
        "bn": "ওই কনফার্মেশন নম্বরে কোনো অ্যাপয়েন্টমেন্ট খুঁজে পেলাম না। নম্বরটা আরেকবার বলবেন?",
        "hi": "उस कन्फर्मेशन नंबर पर कोई अपॉइंटमेंट नहीं मिला। नंबर दोबारा बताएँगे?",
        "en": "I could not find an appointment with that confirmation number. Could you say it again?",
    },
    "reschedule.cancelled": {
        "bn": "ওই অ্যাপয়েন্টমেন্টটা আগেই বাতিল হয়ে গেছে। নতুন করে বুক করে দেব?",
        "hi": "वह अपॉइंटमेंट पहले ही रद्द हो चुका है। नया बुक कर दूँ?",
        "en": "That appointment was already cancelled. Shall I book a new one?",
    },
    "reschedule.no_day": {
        "bn": "ডাক্তার ওই দিন বসছেন না। অন্য কোনো দিন দেখব?",
        "hi": "डॉक्टर उस दिन नहीं बैठ रहे। कोई और दिन देखूँ?",
        "en": "The doctor does not sit that day. Shall I look at another day?",
    },
    "reschedule.slot_taken_alts": {
        "bn": "ওই সময়টা ফাঁকা নেই। এই সময়গুলো আছে: {alts}। কোনটা চান?",
        "hi": "वह समय खाली नहीं है। ये समय हैं: {alts}। कौन सा चाहिए?",
        "en": "That time is not free. These are available: {alts}. Which would you like?",
    },
    "reschedule.slot_taken_none": {
        "bn": "ওই সময়টা ফাঁকা নেই, এবং কাছাকাছি কোনো সময়ও নেই।",
        "hi": "वह समय खाली नहीं है, और आसपास कोई समय भी नहीं है।",
        "en": "That time is not free, and there is nothing nearby either.",
    },
    "reschedule.failed": {
        "bn": "দুঃখিত, অ্যাপয়েন্টমেন্টটা বদলানো গেল না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
        "hi": "माफ़ कीजिए, अपॉइंटमेंट बदला नहीं जा सका। कृपया काउंटर पर संपर्क करें।",
        "en": "Sorry, the appointment could not be moved. Please contact the counter.",
    },
    "cancel.success": {
        "bn": "আপনার অ্যাপয়েন্টমেন্ট বাতিল করা হয়েছে। {date}, সময় {time}। কনফার্মেশন নম্বর: {cid}।",
        "hi": "आपका अपॉइंटमेंट रद्द कर दिया गया है। {date}, समय {time}। कन्फर्मेशन नंबर: {cid}।",
        "en": "Your appointment has been cancelled. {date}, at {time}. Confirmation number: {cid}.",
    },
    "cancel.already": {
        "bn": "ওই অ্যাপয়েন্টমেন্টটা আগেই বাতিল করা হয়েছে। {date} তারিখের কিছু আর বুক করা নেই।",
        "hi": "वह अपॉइंटमेंट पहले ही रद्द किया जा चुका है। {date} को कुछ बुक नहीं है।",
        "en": "That appointment was already cancelled. Nothing is booked for {date}.",
    },
    "cancel.failed": {
        "bn": "দুঃখিত, অ্যাপয়েন্টমেন্টটা বাতিল করা গেল না। কাউন্টারে যোগাযোগ করুন, দয়া করে।",
        "hi": "माफ़ कीजिए, अपॉइंटमेंट रद्द नहीं हो सका। कृपया काउंटर पर संपर्क करें।",
        "en": "Sorry, the appointment could not be cancelled. Please contact the counter.",
    },

    # =======================================================================
    # PAYMENT -- the story's first named flow.
    #
    # Every option below is completable by a person holding a feature phone,
    # or no phone at all. No link is sent, nothing is scanned, and the
    # counter is the ONLY way to pay -- not a fallback for people who
    # "couldn't manage" the digital route, because for most callers on
    # this line it is the normal way to pay.
    #
    # NO ONLINE PAYMENT IS OFFERED, NOT EVEN AS AN OPTION. UPI and every
    # other online method need a smartphone and a data connection, so
    # naming one tells a caller on a basic handset that the "real" route is
    # one they cannot use. Cash or card, handed over at the counter, needs
    # neither. tests/test_no_smartphone.py bans online-payment words in
    # every caller-facing string so an "improvement" cannot bring one back.
    # =======================================================================
    "payment.how": {
        "bn": "টাকা দিতে হবে শুধু কাউন্টারে, আসার দিন — নগদ বা কার্ডে। "
              "ফোনে কোনো টাকা দিতে হবে না।",
        "hi": "पैसे सिर्फ़ काउंटर पर देने होंगे, आने के दिन — नकद या कार्ड से। "
              "फ़ोन पर कोई भुगतान नहीं करना है।",
        "en": "Payment is made only at the counter, on the day you come — in cash or by card. "
              "You do not need to pay anything by phone.",
    },
    "payment.amount": {
        "bn": " {name}-এর জন্য {rate} টাকা লাগবে।",
        "hi": " {name} के लिए {rate} रुपये लगेंगे।",
        "en": " {name} costs {rate} rupees.",
    },
    "payment.no_advance": {
        "bn": " অ্যাপয়েন্টমেন্ট রাখতে আগাম টাকা লাগে না।",
        "hi": " अपॉइंटमेंट रखने के लिए कोई अग्रिम राशि नहीं लगती।",
        "en": " No advance payment is needed to hold the appointment.",
    },
    "payment.counter_only": {
        "bn": " রসিদ কাউন্টারেই ছাপিয়ে হাতে দেওয়া হবে।",
        "hi": " रसीद काउंटर पर ही छापकर हाथ में दी जाएगी।",
        "en": " A printed receipt is handed to you at the counter.",
    },

    # =======================================================================
    # REPORT COLLECTION -- the story's second named flow.
    #
    # A printed copy at the counter is the primary path. The phone readout
    # exists so a caller who cannot travel is not stranded either.
    # =======================================================================
    "report.when": {
        "bn": "রিপোর্ট তৈরি হতে {hours} ঘণ্টা লাগে।",
        "hi": "रिपोर्ट तैयार होने में {hours} घंटे लगते हैं।",
        "en": "The report takes {hours} hours to be ready.",
    },
    "report.when_unknown": {
        "bn": "রিপোর্ট কখন তৈরি হবে সেটা টেস্টের উপর নির্ভর করে।",
        "hi": "रिपोर्ट कब तैयार होगी यह टेस्ट पर निर्भर करता है।",
        "en": "When the report is ready depends on the test.",
    },
    "report.collect": {
        "bn": " ছাপানো কপি কাউন্টার থেকে নিয়ে যেতে পারেন — নাম আর ফোন নম্বর বললেই হবে, "
              "রেফারেন্স নম্বর মনে না থাকলেও চলবে।",
        "hi": " छपी हुई कॉपी काउंटर से ले जा सकते हैं — नाम और फ़ोन नंबर बता दीजिए, "
              "रेफरेंस नंबर याद न हो तो भी चलेगा।",
        "en": " You can collect a printed copy from the counter — just give your name and "
              "phone number; you do not need to remember the reference number.",
    },
    "report.phone_readout": {
        "bn": " আসতে না পারলে ফোন করে জেনে নিতে পারেন, আমরা পড়ে শোনাব।",
        "hi": " आ न सकें तो फ़ोन करके पूछ लीजिए, हम पढ़कर सुना देंगे।",
        "en": " If you cannot come in, call us and we will read it out to you.",
    },
    "report.someone_else": {
        "bn": " অন্য কেউ এসে নিতে চাইলে রোগীর নাম আর ফোন নম্বরটা জানলেই হবে।",
        "hi": " कोई और लेने आए तो मरीज़ का नाम और फ़ोन नंबर पता होना काफ़ी है।",
        "en": " If someone else collects it, they only need the patient's name and phone number.",
    },

    # =======================================================================
    # COUNTER -- the universal completion path.
    # Spoken whenever a flow cannot finish over the phone, so that no turn
    # ends with the caller holding nothing.
    # =======================================================================
    "counter.hours": {
        "bn": "কাউন্টার খোলা থাকে {hours}।",
        "hi": "काउंटर {hours} खुला रहता है।",
        "en": "The counter is open {hours}.",
    },
    "counter.walk_in": {
        "bn": "সরাসরি কাউন্টারে চলে এলেও হবে, আগে থেকে কিছু লাগবে না।",
        "hi": "सीधे काउंटर पर आ जाइए, पहले से कुछ नहीं चाहिए।",
        "en": "You can simply come to the counter; nothing is needed in advance.",
    },

    # =======================================================================
    # PATIENT HISTORY -- disclosed only after verification
    # Author: Chakravardhan
    #
    # THE WORDING IS PART OF THE SECURITY. Every failure sentence below is
    # identical regardless of WHY it failed -- wrong PIN, unknown number, a
    # patient with no factor on file. Saying "we have no record of that
    # number" would confirm whether a named person attends this clinic,
    # which is itself information about them.
    #
    # Nor does any sentence say how many attempts remain. That is a
    # countdown for somebody guessing and useless to somebody who simply
    # mistyped.
    # =======================================================================
    "history.ask_pin": {
        "bn": "আপনার হিস্ট্রি বলার আগে একটু নিশ্চিত হয়ে নিই। কাউন্টার থেকে নেওয়া "
              "আপনার চার সংখ্যার পিনটা বলবেন?",
        "hi": "आपकी हिस्ट्री बताने से पहले पुष्टि कर लेता हूँ। काउंटर से लिया हुआ "
              "आपका चार अंकों का पिन बताएँगे?",
        "en": "Before I read your history, let me confirm it is you. Could you say "
              "your four-digit PIN from the counter?",
    },
    "history.ask_dob": {
        "bn": "আপনার হিস্ট্রি বলার আগে একটু নিশ্চিত হয়ে নিই। আপনার জন্মতারিখটা বলবেন?",
        "hi": "आपकी हिस्ट्री बताने से पहले पुष्टि कर लेता हूँ। आपकी जन्मतिथि बताएँगे?",
        "en": "Before I read your history, let me confirm it is you. Could you tell me "
              "your date of birth?",
    },
    # Spoken for a wrong answer, an unknown number, AND a patient with no
    # factor on file. One sentence for all three, by design.
    "history.retry": {
        "bn": "ওটা মিলল না। আরেকবার বলবেন?",
        "hi": "वह मेल नहीं खाया। एक बार फिर बताएँगे?",
        "en": "That did not match. Could you say it again?",
    },
    "history.failed": {
        "bn": "দুঃখিত, ফোনে নিশ্চিত করতে পারলাম না। কাউন্টারে গিয়ে জিজ্ঞেস করলে "
              "ওঁরা দেখে দেবেন — সঙ্গে কিছু আনতে হবে না, শুধু নিজের নামটা বললেই হবে।",
        "hi": "माफ़ कीजिए, फ़ोन पर पुष्टि नहीं कर सका। काउंटर पर पूछ लीजिए, वे देख "
              "देंगे — कुछ लाने की ज़रूरत नहीं, बस अपना नाम बता दीजिए।",
        "en": "Sorry, I could not confirm that over the phone. Please ask at the "
              "counter and they will look it up — you need bring nothing, just give "
              "your name.",
    },
    "history.locked": {
        "bn": "নিরাপত্তার জন্য এই নম্বরে আপাতত হিস্ট্রি বলা বন্ধ রাখছি। "
              "কাউন্টারে গেলে ওঁরা সঙ্গে সঙ্গে দেখে দেবেন।",
        "hi": "सुरक्षा के लिए इस नंबर पर फ़िलहाल हिस्ट्री बताना बंद रखा है। "
              "काउंटर पर जाइए, वे तुरंत देख देंगे।",
        "en": "For safety I have paused history on this number for now. The counter "
              "can look it up for you straight away.",
    },
    # Spoken when the ROOM is not private, not when the caller is not
    # verified. Names the fix, because it is one the caller can act on.
    "history.speakerphone": {
        "bn": "এটা তো স্পিকারে আছে মনে হচ্ছে। ব্যক্তিগত কথা তাই বলছি না — "
              "ফোনটা কানে নিয়ে আবার বলুন, তাহলে বলে দেব।",
        "hi": "यह स्पीकर पर लग रहा है। निजी बात इसलिए नहीं बता रहा — फ़ोन कान पर "
              "लगाकर फिर बोलिए, तब बता दूँगा।",
        "en": "This sounds like it is on speaker. I will not read anything private "
              "aloud — hold the phone to your ear and say that again, and I will.",
    },
    "history.disclosure_off": {
        "bn": "হিস্ট্রি ফোনে বলা হয় না। কাউন্টারে গেলে ওঁরা দেখে দেবেন।",
        "hi": "हिस्ट्री फ़ोन पर नहीं बताई जाती। काउंटर पर वे देख देंगे।",
        "en": "History is not read out over the phone. The counter can look it up for you.",
    },
    "history.none": {
        "bn": "আপনার নামে এখনও কোনো টেস্টের রেকর্ড নেই।",
        "hi": "आपके नाम पर अभी कोई टेस्ट रिकॉर्ड नहीं है।",
        "en": "There is no test on record for you yet.",
    },
    "history.intro": {
        "bn": "আপনার নামে {count}টি টেস্টের রেকর্ড আছে। ",
        "hi": "आपके नाम पर {count} टेस्ट रिकॉर्ड हैं। ",
        "en": "I have {count} test records for you. ",
    },
    "history.item_ready": {
        "bn": "{date} তারিখে {name}, রিপোর্ট তৈরি। ",
        "hi": "{date} को {name}, रिपोर्ट तैयार है। ",
        "en": "{name} on {date}, report ready. ",
    },
    "history.item_pending": {
        "bn": "{date} তারিখে {name}, রিপোর্ট এখনও তৈরি হয়নি। ",
        "hi": "{date} को {name}, रिपोर्ट अभी तैयार नहीं। ",
        "en": "{name} on {date}, report not ready yet. ",
    },
    "history.more": {
        "bn": "আরও {count}টি আছে — বাকিগুলো কাউন্টারে দেখে নিতে পারেন।",
        "hi": "और {count} हैं — बाकी काउंटर पर देख लीजिए।",
        "en": "There are {count} more — the counter can show you the rest.",
    },
    "history.detail_at_counter": {
        "bn": " রিপোর্টের বিস্তারিত ফোনে বলা হয় না, কাউন্টার থেকে ছাপানো কপি নিয়ে নেবেন।",
        "hi": " रिपोर्ट का विवरण फ़ोन पर नहीं बताया जाता, काउंटर से छपी कॉपी ले लीजिए।",
        "en": " Report details are not read out over the phone; collect a printed copy "
              "at the counter.",
    },

    # =======================================================================
    # A SINGLE PATIENT TIMELINE -- Author: Chakravardhan
    #
    # "I want the agent to already know what I have booked here, so that I
    # am not made to recite my own history to the hospital that holds it."
    #
    # Read only after the same verification and private-room check as the
    # history above. No sentence here says a confirmation number: the point
    # of the story is that the patient no longer needs one to be answered.
    # =======================================================================
    "timeline.ask_pin": {
        "bn": "আপনার বুকিংগুলো বলার আগে একটু নিশ্চিত হয়ে নিই। কাউন্টার থেকে নেওয়া "
              "আপনার চার সংখ্যার পিনটা বলবেন?",
        "hi": "आपकी बुकिंग बताने से पहले पुष्टि कर लेता हूँ। काउंटर से लिया हुआ "
              "आपका चार अंकों का पिन बताएँगे?",
        "en": "Before I tell you your bookings, let me confirm it is you. Could you say "
              "your four-digit PIN from the counter?",
    },
    "timeline.ask_dob": {
        "bn": "আপনার বুকিংগুলো বলার আগে একটু নিশ্চিত হয়ে নিই। আপনার জন্মতারিখটা বলবেন?",
        "hi": "आपकी बुकिंग बताने से पहले पुष्टि कर लेता हूँ। आपकी जन्मतिथि बताएँगे?",
        "en": "Before I tell you your bookings, let me confirm it is you. Could you tell me "
              "your date of birth?",
    },
    # Asked only when the call does not yet know which record to look at.
    # The number LOCATES the record; the challenge that follows is what
    # proves who is asking. Asked the same way whether or not the number is
    # known to the clinic -- see history.retry for why that matters.
    "timeline.ask_phone": {
        "bn": "আপনার রেকর্ড কোন ফোন নম্বরে আছে, সেটা বলবেন?",
        "hi": "आपका रिकॉर्ड किस फ़ोन नंबर पर है, वह बताएँगे?",
        "en": "Which phone number is your record under?",
    },
    "timeline.no_bookings": {
        "bn": "এই মুহূর্তে আপনার নামে সামনে কোনো অ্যাপয়েন্টমেন্ট বুক করা নেই।",
        "hi": "अभी आपके नाम पर आगे का कोई अपॉइंटमेंट बुक नहीं है।",
        "en": "You have no upcoming appointment booked with us.",
    },
    "timeline.intro": {
        "bn": "আপনার {count}টি অ্যাপয়েন্টমেন্ট বুক করা আছে। ",
        "hi": "आपके {count} अपॉइंटमेंट बुक हैं। ",
        "en": "Appointments booked for you: {count}. ",
    },
    "timeline.item": {
        "bn": "{doctor}, {date}, সময় {time}। ",
        "hi": "{doctor}, {date}, समय {time}। ",
        "en": "{doctor}, {date}, at {time}. ",
    },
    "timeline.more": {
        "bn": "আরও {count}টি আছে — কাউন্টারে দেখে নিতে পারেন।",
        "hi": "और {count} हैं — काउंटर पर देख लीजिए।",
        "en": "There are {count} more — the counter can tell you the rest.",
    },
    # Appended to a booking confirmation when the name and number came from
    # the verified record rather than from the caller. Says THAT the record
    # was used, never what is in it -- the name is not read back.
    "timeline.used_record": {
        "bn": " আপনার রেকর্ডে থাকা নাম আর নম্বরেই বুক করেছি।",
        "hi": " आपके रिकॉर्ड में दर्ज नाम और नंबर पर ही बुक किया है।",
        "en": " I have booked it under the name and number already on your record.",
    },

    # =======================================================================
    # THE SAME QUESTIONS BY MESSAGE -- Author: Chakravardhan
    #
    # "I want to ask the same questions by message and get the same answers,
    # so that I can use the channel I already have open."
    #
    # Every ANSWER on the message channel is the sentence above, unchanged.
    # These are only what the channel itself needs to say.
    # =======================================================================
    # History and bookings are never written into a message -- see
    # agent/privacy.py channel_is_private(). Points to the two paths that
    # can: the phone line and the counter.
    "channel.private_by_message": {
        "bn": "আপনার গোপনীয়তার জন্য মেসেজে আপনার রেকর্ড বা বুকিংয়ের তথ্য দিতে পারি না। "
              "ফোন করে জিজ্ঞেস করুন, অথবা কাউন্টারে যোগাযোগ করুন।",
        "hi": "आपकी गोपनीयता के लिए मैसेज में आपका रिकॉर्ड या बुकिंग की जानकारी नहीं दे सकता। "
              "कृपया फ़ोन करके पूछें, या काउंटर पर संपर्क करें।",
        "en": "For your privacy, I cannot share your records or bookings in a message. "
              "Please call us and ask, or contact the counter.",
    },
    # Prefixed to the next question when a booking started on the other
    # channel is picked up here.
    "channel.resumed": {
        "bn": "আগে যে বুকিংটা শুরু করেছিলেন, সেখান থেকেই চালিয়ে যাচ্ছি। ",
        "hi": "आपने पहले जो बुकिंग शुरू की थी, वहीं से आगे बढ़ते हैं। ",
        "en": "Let us continue the booking you started earlier. ",
    },
    # A voice note, a photo, a sticker. Nothing here can read those.
    "channel.text_only": {
        "bn": "এখানে শুধু লেখা মেসেজ পড়তে পারি। প্রশ্নটা লিখে পাঠান, অথবা ফোন করুন।",
        "hi": "यहाँ मैं सिर्फ़ लिखे हुए मैसेज पढ़ सकता हूँ। अपना सवाल लिखकर भेजें, या फ़ोन करें।",
        "en": "I can only read typed messages here. Please type your question, or call us.",
    },

    # =======================================================================
    # LANGUAGE
    # =======================================================================
    "language.switched": {
        "bn": "ঠিক আছে, বাংলাতেই বলছি।",
        "hi": "ठीक है, अब हिंदी में बात करता हूँ।",
        "en": "Alright, I will continue in English.",
    },
    "language.unavailable": {
        "bn": "দুঃখিত, এই লাইনে এখন শুধু {available} বলতে পারি।",
        "hi": "माफ़ कीजिए, इस लाइन पर अभी सिर्फ़ {available} में बात कर सकता हूँ।",
        "en": "Sorry, on this line I can currently speak only {available}.",
    },
}


class MissingString(KeyError):
    """A key was asked for that no language defines. Raised only from
    strict_check(); t() degrades instead, because a missing translation must
    never end a call."""


def t(lang: str, key: str, **fmt) -> str:
    """-> the sentence for `key` in `lang`, formatted with `fmt`.

    NEVER raises for an unknown language, an unknown key or a missing
    placeholder. A caller is on a live line; a KeyError here would drop the
    turn, and an English sentence in a Bengali call is a far smaller failure
    than silence.

    Falls back in this order:
        requested language -> default language -> English -> the key itself
    """
    entry = _STRINGS.get(key)
    if entry is None:
        return key

    code = lang_mod.resolve(lang)
    text = entry.get(code) or entry.get(lang_mod.default_lang()) or entry.get("en")
    if text is None:
        return key
    try:
        return text.format(**fmt)
    except (KeyError, IndexError):
        # A placeholder the caller of t() did not supply. Return the
        # unformatted sentence rather than crash: the caller still hears
        # something coherent, and the gap shows up in review.
        return text


def available_languages_phrase(lang: str) -> str:
    """The native names of what this line can actually speak, joined for
    speech. Used by language.unavailable so the caller is told what IS on
    offer, not merely what is not."""
    names = [lang_mod.SPECS[c].native_name for c in lang_mod.enabled() if c in lang_mod.SPECS]
    if not names:
        names = [lang_mod.SPECS[lang_mod.default_lang()].native_name]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + (" / " if lang == "en" else " আর " if lang == "bn" else " और ") + names[-1]


def all_strings() -> list[tuple[str, str, str]]:
    """-> (key, lang, text) for every string in the table.

    Exists for tests/test_no_smartphone.py, which walks all of them
    asserting no caller-facing sentence requires a smartphone. Exposed as a
    function rather than by importing _STRINGS so the test does not depend
    on the table's shape.
    """
    return [(key, code, text)
            for key, entry in _STRINGS.items()
            for code, text in entry.items()]


def strict_check() -> list[str]:
    """-> a list of complaints about the table: keys missing a language.

    Not called at import. Translation gaps are survivable at runtime (t()
    falls back) but should fail review, so this is asserted in the test
    suite instead of crashing a pod at 3am over a missing Hindi string.
    """
    problems = []
    for key, entry in _STRINGS.items():
        for code in lang_mod.ALL_LANGS:
            if not entry.get(code):
                problems.append(f"{key}: no {code}")
    return problems

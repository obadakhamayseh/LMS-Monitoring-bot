import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(message)s"
)

def test_lms():
    from lms_scraper import LMSScraper

    print("=" * 50)
    print("🧪 اختبار الاتصال بنظام LMS")
    print("=" * 50)

    username = input("أدخل رقم الطالب: ").strip()
    password = input("أدخل كلمة المرور: ").strip()

    print("\n⏳ جارٍ تسجيل الدخول...")
    scraper = LMSScraper(username, password)

    if scraper.login():
        print(f"✅ تسجيل الدخول ناجح!")
        print(f"   الاسم: {scraper.full_name}")
        print(f"   User ID: {scraper.user_id}")

        print("\n⏳ جارٍ جلب المواد...")
        courses = scraper.get_courses()

        if courses:
            print(f"\n✅ تم جلب {len(courses)} مادة:")
            for i, course in enumerate(courses, 1):
                print(f"   {i}. [{course['id']}] {course['name']}")

            print(f"\n⏳ جارٍ جلب محتوى المادة الأولى: {courses[0]['name']}...")
            content = scraper.get_course_content(courses[0]['id'])

            print(f"\n📚 الواجبات ({len(content['assignments'])}):")
            for a in content['assignments']:
                print(f"   - {a['name']} | {a['url']}")

            print(f"\n📝 الكويزات ({len(content['quizzes'])}):")
            for q in content['quizzes']:
                print(f"   - {q['name']} | {q['url']}")

            print(f"\n📎 الموارد ({len(content['resources'])}):")
            for r in content['resources'][:5]:
                print(f"   - {r['name']} | {r['url']}")
        else:
            print("⚠️ لم يتم العثور على مواد دراسية")
    else:
        print("❌ فشل تسجيل الدخول - تحقق من البيانات")

    print("\n" + "=" * 50)
    print("اكتمل الاختبار")
    print("=" * 50)

if __name__ == "__main__":
    test_lms()

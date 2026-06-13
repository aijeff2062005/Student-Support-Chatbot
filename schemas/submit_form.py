from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

# ============ Enums ============


class NotificationChannel(StrEnum):
    """Notification channels"""

    EMAIL = "email"
    ZALO = "zalo"
    MESSENGER = "messenger"
    WHATSAPP = "whatsapp"


class Grade(StrEnum):
    """Student grade levels"""

    GRADE_10 = "10"
    GRADE_11 = "11"
    GRADE_12 = "12"
    GRADUATED = "graduated"

    @property
    def label(self) -> str:
        labels = {"10": "Lớp 10", "11": "Lớp 11", "12": "Lớp 12", "graduated": "Đã tốt nghiệp"}
        return labels[self.value]


class Gender(StrEnum):
    """Gender options"""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"

    @property
    def label(self) -> str:
        labels = {"male": "Nam", "female": "Nữ", "other": "Khác"}
        return labels[self.value]


class AcademicPerformance(StrEnum):
    """Academic performance levels"""

    EXCELLENT = "excellent"  # Giỏi
    GOOD = "good"  # Khá
    AVERAGE = "average"  # Trung bình
    WEAK = "weak"  # Yếu

    @property
    def label(self) -> str:
        labels = {"excellent": "Giỏi", "good": "Khá", "average": "Trung bình", "weak": "Yếu"}
        return labels[self.value]

    @property
    def short_label(self) -> str:
        """Short label without score range"""
        labels = {"excellent": "Giỏi", "good": "Khá", "average": "Trung bình", "weak": "Yếu"}
        return labels[self.value]


class Industry(StrEnum):
    """Industry/Field categories"""

    IT = "cong-nghe-thong-tin"
    MEDIA_MARKETING = "truyen-thong-marketing"
    BUSINESS_SALES = "kinh-doanh-ban-hang"
    HOSPITALITY = "du-lich-nha-hang-khach-san"
    LOGISTICS = "logistics-chuoi-cung-ung"
    FINANCE_BANKING = "tai-chinh-ngan-hang"
    LAW = "luat"
    LINGUISTICS = "ngon-ngu-hoc"
    SOCIAL_HUMANITIES = "khoa-hoc-xa-hoi-nhan-van"
    MEDICINE = "y-hoc"

    @property
    def label(self) -> str:
        labels = {
            "cong-nghe-thong-tin": "Công Nghệ Thông Tin",
            "truyen-thong-marketing": "Truyền Thông - Marketing",
            "kinh-doanh-ban-hang": "Kinh Doanh - Bán Hàng",
            "du-lich-nha-hang-khach-san": "Du Lịch - Nhà Hàng - Khách Sạn",
            "logistics-chuoi-cung-ung": "Logistics & Chuỗi Cung Ứng",
            "tai-chinh-ngan-hang": "Tài Chính - Ngân Hàng",
            "luat": "Luật",
            "ngon-ngu-hoc": "Ngôn Ngữ Học",
            "khoa-hoc-xa-hoi-nhan-van": "Khoa Học Xã Hội & Nhân Văn",
            "y-hoc": "Y Học",
        }
        return labels[self.value]


class Major(StrEnum):
    """Academic majors"""

    # IT majors
    INFORMATION_TECHNOLOGY = "information-technology"
    SOFTWARE_ENGINEERING = "software-engineering"
    COMPUTER_SCIENCE = "computer-science"
    ARTIFICIAL_INTELLIGENCE = "artificial-intelligence"
    DATA_SCIENCE = "data-science"
    CYBER_SECURITY = "cyber-security"

    # Business majors
    BUSINESS_ADMINISTRATION = "business-administration"
    ACCOUNTANT = "accountant"
    MARKETING = "marketing"
    FINANCE = "finance"
    INTERNATIONAL_BUSINESS = "international-business"

    # Creative majors
    GRAPHIC_DESIGN = "graphic-design"
    MULTIMEDIA_DESIGN = "multimedia-design"
    UI_UX_DESIGN = "ui-ux-design"

    # Hospitality & Tourism
    HOTEL_MANAGEMENT = "hotel-management"
    TOURISM_MANAGEMENT = "tourism-management"

    # Others
    LOGISTICS = "logistics"
    ENGLISH = "english"
    LAW = "law"

    @property
    def label(self) -> str:
        labels = {
            "information-technology": "Công nghệ Thông tin",
            "software-engineering": "Kỹ thuật Phần mềm",
            "computer-science": "Khoa học Máy tính",
            "artificial-intelligence": "AI – Trí tuệ Nhân tạo",
            "data-science": "Khoa học Dữ liệu",
            "cyber-security": "An toàn Thông tin",
            "business-administration": "Quản trị Kinh doanh",
            "accountant": "Kế toán",
            "marketing": "Marketing",
            "finance": "Tài chính",
            "international-business": "Kinh doanh Quốc tế",
            "graphic-design": "Thiết kế Đồ họa",
            "multimedia-design": "Thiết kế Đa phương tiện",
            "ui-ux-design": "Thiết kế UI/UX",
            "hotel-management": "Quản trị Khách sạn",
            "tourism-management": "Quản trị Du lịch",
            "logistics": "Logistics & Chuỗi cung ứng",
            "english": "Ngôn ngữ Anh",
            "law": "Luật",
        }
        return labels[self.value]

    @property
    def industry(self) -> Industry:
        """Get industry for this major"""
        mapping = {
            Major.INFORMATION_TECHNOLOGY: Industry.IT,
            Major.SOFTWARE_ENGINEERING: Industry.IT,
            Major.COMPUTER_SCIENCE: Industry.IT,
            Major.ARTIFICIAL_INTELLIGENCE: Industry.IT,
            Major.DATA_SCIENCE: Industry.IT,
            Major.CYBER_SECURITY: Industry.IT,
            Major.BUSINESS_ADMINISTRATION: Industry.BUSINESS_SALES,
            Major.ACCOUNTANT: Industry.FINANCE_BANKING,
            Major.MARKETING: Industry.MEDIA_MARKETING,
            Major.FINANCE: Industry.FINANCE_BANKING,
            Major.INTERNATIONAL_BUSINESS: Industry.BUSINESS_SALES,
            Major.GRAPHIC_DESIGN: Industry.MEDIA_MARKETING,
            Major.MULTIMEDIA_DESIGN: Industry.MEDIA_MARKETING,
            Major.UI_UX_DESIGN: Industry.MEDIA_MARKETING,
            Major.HOTEL_MANAGEMENT: Industry.HOSPITALITY,
            Major.TOURISM_MANAGEMENT: Industry.HOSPITALITY,
            Major.LOGISTICS: Industry.LOGISTICS,
            Major.ENGLISH: Industry.LINGUISTICS,
            Major.LAW: Industry.LAW,
        }
        return mapping[self]


class ParentRelation(StrEnum):
    """Parent/Guardian relationship"""

    FATHER = "father"
    MOTHER = "mother"
    GUARDIAN = "guardian"

    @property
    def label(self) -> str:
        labels = {"father": "Cha", "mother": "Mẹ", "guardian": "Người giám hộ"}
        return labels[self.value]


class EventSlot(StrEnum):
    """Event time slots"""

    MORNING = "sang"
    AFTERNOON = "chieu"
    FULL_DAY = "ca-ngay"

    @property
    def label(self) -> str:
        labels = {"sang": "Buổi sáng (8:00 - 12:00)", "chieu": "Buổi chiều (13:30 - 17:30)", "ca-ngay": "Cả ngày"}
        return labels[self.value]


class HeardFrom(StrEnum):
    """How user heard about the event"""

    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FRIEND = "ban-be"
    HIGH_SCHOOL = "truong-thpt"
    WEBSITE = "website"
    MEDIA = "bao-chi"
    OTHER = "khac"

    @property
    def label(self) -> str:
        labels = {
            "facebook": "Facebook",
            "tiktok": "TikTok",
            "instagram": "Instagram",
            "ban-be": "Bạn bè giới thiệu",
            "truong-thpt": "Trường THPT",
            "website": "Website trường",
            "bao-chi": "Báo chí / Truyền thông",
            "khac": "Khác",
        }
        return labels[self.value]


# ============ Dataclasses ============


@dataclass
class EventObjective:
    """Pre-defined event objectives"""

    LEARN_MAJORS = "Tìm hiểu về các ngành học"
    TOUR_CAMPUS = "Tham quan trường đại học"
    SCHOLARSHIP_INFO = "Tìm hiểu học bổng và chính sách hỗ trợ"
    EXPLORE_ENVIRONMENT = "Khám phá môi trường học tập"
    MEET_FACULTY = "Gặp gỡ giảng viên và sinh viên"

    @classmethod
    def all(cls) -> list[str]:
        return [cls.LEARN_MAJORS, cls.TOUR_CAMPUS, cls.SCHOLARSHIP_INFO, cls.EXPLORE_ENVIRONMENT, cls.MEET_FACULTY]


@dataclass
class EventSession:
    """Individual event session"""

    id: str
    club_name: str
    event_name: str
    date: date
    date_display: str
    slot: EventSlot
    location: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "clubName": self.club_name,
            "eventName": self.event_name,
            "date": self.date.isoformat(),
            "dateDisplay": self.date_display,
            "slotLabel": self.slot.label,
            "slotValue": self.slot.value,
            "location": self.location,
        }


@dataclass
class DefaultConfig:
    """Default configuration values"""

    DEFAULT_CLUB_NAME: str = "Câu lạc bộ Robotics"
    DEFAULT_LOCATION_TAN_MY: str = "Cơ sở Tân Mỹ, TP.HCM"
    DEFAULT_LOCATION_Q9: str = "Cơ sở Quận 9, TP.HCM"

    # Pre-defined event sessions
    EVENT_SESSIONS: list[EventSession] = field(
        default_factory=lambda: [
            EventSession(
                id="tvts-1503-sang",
                club_name="Câu lạc bộ Robotics",
                event_name="Ngày hội tư vấn tuyển sinh 2025",
                date=date(2025, 3, 15),
                date_display="15/03/2025",
                slot=EventSlot.MORNING,
                location="Cơ sở Tân Mỹ, TP.HCM",
            ),
            EventSession(
                id="tvts-1503-chieu",
                club_name="Câu lạc bộ Robotics",
                event_name="Ngày hội tư vấn tuyển sinh 2025",
                date=date(2025, 3, 15),
                date_display="15/03/2025",
                slot=EventSlot.AFTERNOON,
                location="Cơ sở Tân Mỹ, TP.HCM",
            ),
            EventSession(
                id="openday-2203",
                club_name="Câu lạc bộ Robotics",
                event_name="Open Day - Công nghệ Thông tin",
                date=date(2025, 3, 22),
                date_display="22/03/2025",
                slot=EventSlot.FULL_DAY,
                location="Cơ sở Quận 9, TP.HCM",
            ),
        ]
    )


def enum_to_options(enum_class) -> list[dict]:
    """Convert enum to options list for API response"""
    return [{"value": item.value, "label": item.label} for item in enum_class]


def get_majors_by_industry(industry: Industry) -> list[Major]:
    """Get all majors in a specific industry"""
    return [major for major in Major if major.industry == industry]


@dataclass
class ApplicationFormData:
    """Enrollment form submission data"""

    # Personal Information
    full_name: str | None = None
    gender: str | None = None  # "nam", "nu", "khac", "__empty"
    birth_date: date | None = None
    national_id: str | None = None
    student_phone: str | None = None
    parent_phone: str | None = None
    email: str | None = None
    majors: str | None = None
    specializations: str | None = None
    admission_methods: str | None = None

    # Permanent Address
    permanent_province: str | None = None
    permanent_ward: str | None = None
    permanent_street: str | None = None
    permanent_house: str | None = None

    # Academic Information (Grade 12)
    grade12_province: str | None = None
    grade12_school: str | int | None = None  # School ID
    grade12_class: str | None = None
    graduation_year: str | None = None

    # Receiving Address
    receiving_province: str | None = None
    receiving_ward: str | None = None
    receiving_street: str | None = None
    receiving_house: str | None = None

    # Other
    apply_same_address: bool = True
    confirm_accuracy: bool = True
    section_id: str | None = None  # conversation_id/session_id
    platform: str | None = None

    def to_query_params(self) -> dict:
        """Convert to query parameters for API submission"""
        params = {}

        # Map dataclass fields to API parameter names
        field_mapping = {
            "full_name": "fullName",
            "gender": "gender",
            "birth_date": "birthDate",
            "national_id": "nationalId",
            "student_phone": "studentPhone",
            "parent_phone": "parentPhone",
            "email": "email",
            "permanent_province": "permanentProvince",
            "permanent_ward": "permanentWard",
            "permanent_street": "permanentStreet",
            "permanent_house": "permanentHouse",
            "grade12_province": "grade12Province",
            "grade12_school": "grade12School",
            "grade12_class": "grade12Class",
            "graduation_year": "graduationYear",
            "receiving_province": "receivingProvince",
            "receiving_ward": "receivingWard",
            "receiving_street": "receivingStreet",
            "receiving_house": "receivingHouse",
            "apply_same_address": "applySameAddress",
            "confirm_accuracy": "confirmAccuracy",
            "section_id": "section_id",
            "platform": "platform",
            "majors": "majors",
            "specializations": "specializations",
            "admission_methods": "admissionMethods",
        }

        for field_name, param_name in field_mapping.items():
            value = getattr(self, field_name)

            if value is None:
                # Handle defaults for specific fields when they are None
                if field_name == "apply_same_address":
                    params[param_name] = "true"
                elif field_name == "confirm_accuracy":
                    params[param_name] = "true"
                else:
                    params[param_name] = "__empty"
            else:
                # Convert date to string format
                if isinstance(value, date):
                    params[param_name] = value.isoformat()
                # Convert boolean to string
                elif isinstance(value, bool):
                    params[param_name] = str(value).lower()
                # Convert float to string
                elif isinstance(value, float):
                    params[param_name] = str(value)
                else:
                    # Use __empty if the string is empty, otherwise use the value
                    str_val = str(value)
                    params[param_name] = str_val if str_val else "__empty"

        return params


@dataclass
class EnrollmentFormData:
    """Enrollment form submission data"""

    # Personal Information
    full_name: str | None = None
    birth_date: date | None = None
    national_id: str | None = None
    gender: str | None = None  # "nam", "nu", "khac"
    phone: str | None = None
    email: str | None = None
    address: str | None = None

    # Academic Information
    high_school: str | None = None
    grade_level: str | None = None  # "10", "11", "12"
    academic_performance: str | None = None  # "gioi", "kha", "trung-binh", "yeu"
    gpa: float | None = None
    strong_subjects: str | None = None  # Comma-separated subjects
    social_link: str | None = None

    # Major Preferences
    major_preference_1: str | None = None
    major_preference_2: str | None = None
    major_preference_3: str | None = None

    # Notification
    notify_via: str | None = None  # "Email", "Zalo", "Messenger", "WhatsApp"
    confirm_accuracy: bool | None = None

    def to_query_params(self) -> dict:
        """Convert to query parameters for API submission"""
        params = {}

        # Map dataclass fields to API parameter names
        field_mapping = {
            "full_name": "fullName",
            "birth_date": "birthDate",
            "national_id": "nationalId",
            "gender": "gender",
            "phone": "phone",
            "email": "email",
            "address": "address",
            "high_school": "highSchool",
            "grade_level": "gradeLevel",
            "academic_performance": "academicPerformance",
            "gpa": "gpa",
            "strong_subjects": "strongSubjects",
            "social_link": "socialLink",
            "major_preference_1": "majorPreference1",
            "major_preference_2": "majorPreference2",
            "major_preference_3": "majorPreference3",
            "notify_via": "notifyVia",
            "confirm_accuracy": "confirmAccuracy",
        }

        for field_name, param_name in field_mapping.items():
            value = getattr(self, field_name)

            if value is None:
                # Handle defaults for specific fields when they are None
                if field_name == "notify_via":
                    params[param_name] = "Email"
                elif field_name == "confirm_accuracy":
                    params[param_name] = "false"
                else:
                    params[param_name] = "__empty"
            else:
                # Convert date to string format
                if isinstance(value, date):
                    params[param_name] = value.isoformat()
                # Convert boolean to string
                elif isinstance(value, bool):
                    params[param_name] = str(value).lower()
                # Convert float to string
                elif isinstance(value, float):
                    params[param_name] = str(value)
                else:
                    params[param_name] = str(value)

        return params

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    handle: str
    title: str
    reason: str
    enabled: bool = True

    @property
    def username(self) -> str:
        return self.handle.removeprefix("@")

    @property
    def telegram_url(self) -> str:
        return f"https://t.me/{self.username}"


SOURCES: list[Source] = [
    Source("@freelansim_ru", "Хабр Фриланс", "часто публикует подборки в категории боты и парсинг"),
    Source("@digitaltender", "DIGITAL Tender", "крупный канал с digital-заказами и разработкой"),
    Source("@freelancetaverna", "Фриланс Таверна", "много удаленных задач и вакансий для разработчиков"),
    Source("@frilans", "Фриланс | Удаленная работа", "широкий фриланс-канал, фильтр режет нерелевантное"),
    Source("@job_developer", "Фриланс для разработчиков", "разработка, удаленка, фриланс-заказы"),
    Source("@search_techspec", "Ищу Техспец", "заказы для технических специалистов и разработчиков"),
    Source("@search_zakaz", "Ищу Заказы", "агрегатор заказов, полезен для расширения выдачи"),
    Source("@FreeVacanciesIT", "IT Фриланс | Вакансии", "IT-разовая и проектная работа"),
    Source("@freelance_dev_work", "Kwork разработка и IT", "агрегатор Kwork-заказов по разработке"),
    Source("@pixeltechspec", "Pixel | Заказы для Тех-спецов", "проектные заказы для техспецов"),
    Source("@webfrl", "Web Freelance", "web/fullstack задачи, иногда боты и API"),
    Source("@workayte", "Работа в ИТ", "IT-вакансии и удаленные проектные задачи"),
    Source("@FreelancehuntProjects", "Freelancehunt Projects", "лента проектов с биржи Freelancehunt"),
    Source("@itfreelancers", "IT freelance and remote", "проекты, лиды, удалённая работа для IT-специалистов"),
    Source("@kadrof_work", "Вакансии для фрилансеров", "ежедневные заказы и удалёнка, много IT и разработки"),
    Source("@theyseeku", "Finder.work", "крупный канал удалённой работы, часто мелькают фриланс-проекты для разработчиков"),
    Source("@naudalenkebro", "Удаленка 2.0 | Finder.work", "разовые проекты и вакансии, много от студий и агентств"),
    Source("@distantsiya", "Дистанция", "фриланс-проекты и удалёнка, публикация платная → меньше шума"),
    Source("@zapwork", "Удалёнщики", "удалённая работа и фриланс, широкий охват специализаций"),
    Source("@workathomerus", "WorkAtHome", "удалённая работа и фриланс-заказы, включая IT и разработку"),
    Source("@devjobs42", "Доработки42", "задачи от клиентов 42Clouds, в основном разработка и доработки"),
    Source("@getitrussia", "Get IT Russia", "сообщество разработчиков, вакансии и фриланс-предложения"),
    Source("@jobGeeks", "Топ IT Вакансии", "вакансии и фриланс-заказы для разработчиков, DevOps, QA"),
    Source("@geekjobs", "Job in IT&Digital", "эксклюзивные IT-вакансии, в том числе проектные и фриланс"),
    Source("@vacansii_sz", "Вакансии Северо-Запад", "проверенные вакансии, много удалёнки и фриланса"),
    Source("@it_vac", "IT Jobs | вакансии, фриланс", "IT-вакансии в штат и на фриланс"),
    Source("@jc_it", "Jobs Code: IT-вакансии", "IT-вакансии, включая проектную работу и фриланс"),
    Source("@fordev", "Вакансии Backend/Frontend", "вакансии и заказы для backend и frontend разработчиков"),
]


def enabled_sources() -> list[Source]:
    return [source for source in SOURCES if source.enabled]


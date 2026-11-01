# Лаба 3 — Репликация базы данных: подними, сломай, восстанови

**Цель**: поднять репликацию базы данных, подключить к ней сервис, а затем увидеть отставание реплики (replication lag), связь с теоремой CAP и действия при отказе primary (failover).

## PostgreSQL — почему?

1. Мне реляционные таблицы дороже json-ов! После долгих ночей работы с динамической типизацией хочется чего-то стабильного.
2. В PostgreSQL можно контролировать процесс руками. В MongoDB failover происходит сам - подойдет, если захочется понаблюдать.
3. На предыдущих дисциплинах уже был опыт с PostgreSQL со стороны разработчиков и со стороны проектировщиков бд.

## Часть 1 — Поднятие репликации (через конфигурацию)

### Давайте разбираться

> Худая кобыла с места рвёт

Можно было б начинать, однако так сразу не получится, нужно устаканить плавающие знания.
Почитав статейки
([тут](https://dev.to/kalio/postgresql-read-replicas-with-docker-a-step-by-step-guide-1hil), [тут](https://medium.com/swlh/postgresql-replication-with-docker-c6a904becf77) и [тут](https://github.com/sun1211/postgresql-docker-replication)),
понимаем фронт работ и делаем выводы:

- **Репликация или шардирование?** Репликация - это полная копия (или несколько копий) объекта изучения, а шардирование делит данные между узлами. Их, естественно, можно комбинировать. В этой лабе мы не трогаем распределенное хранение, нам нужно только резервирование.
- **Резервирование или резервное копирование?** Тут тоже важно понимать разницу: резервирование подразумевает наличие горячего резерва, способного скорейшим образом подменить упавшую часть, а резервное копирование (бекапы) в результате получает простую копию, которую ещё нужно применить и запустить.
- **Физически или логически? Быть или казаться?** В контексте реплик, логическая реплика кажется копией оригинала, однако не обязательно её является, можно настраивать копирование лишь части таблиц, а также БД может быть запущена на другой архитектуре. В лабе нужна физическая реплика - именно она позволит сделать отказоустойчивость.

Основная механика репликации работает на WAL (журнале предзаписи).
На выбор у нас два режима работы:

- **асинхронный** основная БД не ждет реплику, однако мы можем потерять данные между записью в WAL и чтением изменений из реплики,
- **синхронный** идет без потерь, но основной БД приходится ждать подтверждения реплики.

### Поработаем ручками

Сначала напишем конфигурацию основного сервера [`primary/postgresql.conf`](./config/primary/postgresql.conf). Здесь мы задаем асинхронный режим работы с одной репликой.

```conf
listen_addresses = '*'

# Включать полные изменения строк в WAL
wal_level = replica
# Разрешить до 1 одновременных подключений к репликации
max_wal_senders = 1
# Один слот (предотвращает преждевременное удаление WAL)
max_replication_slots = 1
# Хранить 256 МБ файлов WAL для отстающих реплик
wal_keep_size = 256

# Выполнять сброс на локальный диск перед подтверждением (не на реплику)
synchronous_commit = on

# Performance
shared_buffers = 256MB
work_mem = 4MB
checkpoint_completion_target = 0.9
```

Далее настройки входящих подключений основного сервера [`primary/pg_hba.conf`](./config/primary/pg_hba.conf). Здесь мы разрешаем подключаться реплике.

```conf
# Разрешить локальные подключения
local   all             all                                     trust
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust

# Разрешить реплике подключиться для репликации
host    replication     replicator      0.0.0.0/0               md5

# Разрешить подключения приложений из сети Docker
host    all             all             0.0.0.0/0               md5
```

Затем напишем конфигурацию реплики [`replica/postgresql.conf`](./config/replica/postgresql.conf). Тут просто указываем ей работать в режиме горячей репликации.

```conf
# Включить запросы на чтение при применении изменений WAL
hot_standby = on

#  Сообщает основному серверу, какие строки все еще нужны реплике
hot_standby_feedback = on

# Performance
shared_buffers = 256MB
work_mem = 4MB
```

Также необходим скрипт начальной инициализации реплики, который возьмет снимок основной БД ([`replica/replica-entrypoint.sh`](./config/replica/replica-entrypoint.sh)).

В итоге получаем следующую структуру конфигурационных файлов.

```txt
config/
├── init/
│   ├── 01_init.sql
│   └── 02_schema.sql
├── primary/
│   ├── postgresql.conf
│   └── pg_hba.conf
└── replica/
    ├── postgresql.conf
    ├── pg_hba.conf
    └── replica-entrypoint.sh
```

Наконец, опишем весь стенд в [`docker-compose.yml`](./docker-compose.yml)

<details>
<summary>
    Конфигурация compose для связки primary + replica
</summary>

```yml
name: lab3

services:
    pg-primary:
        image: postgres:18.6
        container_name: pg-primary
        environment:
            POSTGRES_USER: postgres
            POSTGRES_PASSWORD: postgres
            POSTGRES_DB: postgres
        volumes:
            - primary-data:/var/lib/postgresql
            - ./config/primary/postgresql.conf:/etc/primary/postgresql.conf
            - ./config/primary/pg_hba.conf:/etc/primary/pg_hba.conf
            - ./config/init:/docker-entrypoint-initdb.d
        command: >
            postgres
              -c config_file=/etc/primary/postgresql.conf
              -c hba_file=/etc/primary/pg_hba.conf
        ports:
            - "5432:5432"
        healthcheck:
            test: ["CMD-SHELL", "pg_isready -h 0.0.0.0 -p 5432 -U postgres"]
            interval: 5s
            timeout: 5s
            retries: 15
            start_period: 10s

    pg-standby:
        image: postgres:18.6
        container_name: pg-standby
        user: postgres
        environment:
            PRIMARY_HOST: pg-primary
            POSTGRES_PASSWORD: postgres
        volumes:
            - standby-data:/var/lib/postgresql
            - ./config/replica/postgresql.conf:/etc/replica/postgresql.conf
            - ./config/replica/pg_hba.conf:/etc/replica/pg_hba.conf
            - ./config/replica/replica-entrypoint.sh:/replica-entrypoint.sh
        entrypoint: ["/bin/bash", "/replica-entrypoint.sh"]
        ports:
            - "5433:5432"
        depends_on:
            pg-primary:
                condition: service_healthy

volumes:
    primary-data:
    standby-data:
```

</details>

### Чинилы и запуск БД

#### Попытка запуска №1

> [!WARNING]
> Вылезает ошибка из-за смены структуры каталога данных в образе PostgreSQL 18 `Error: in 18+, these Docker images are configured`.
> Проблема решается сменой пути к данным на `/var/lib/postgresql/18/docker`.

#### Попытка запуска №2

> [!WARNING]
> Вылезает ошибка подключения реплики к оригиналу `number of requested standby connections exceeds "max_wal_senders" (currently 1)`.
> Проблема решается повышением числа подключений в конфиге `max_wal_senders = 3`.
> Проблема возникла, так как изначально я не знал, что одной реплике нужно несколько подключений и вписал единицу.

#### Попытка запуска №3

> [!WARNING]
> Вылезает ошибка реплики `cp: -r not specified; omitting directory '/etc/replica/pg_hba.conf'`.
> Проблема возникла, потому что я пытался примонтировать файл конфигурации подключений на реплике, но сам этот файл не создал: compose создал вместо него директорию.
> Проблема решается созданием примитивного [`replica/pg_hba.conf`](./config/replica/pg_hba.conf).

#### Попытка запуска №4

Оба сервера запустились без ошибок, для проверки репликаций были просмотрены следующие параметры.

- `pg-primary`: строка с `state` = `streaming`. Это доказывает, что репликация работает.
- `pg-standby`: строка `t`. Это доказывает, что standby работает как реплика, а не как отдельная база.

![Успешная работа реплики](./graphics/1-succesfully.png)

Далее проверим запись на primary и чтение с реплики.

![Успешная запись на primary и чтение с реплики](./graphics/1-rw.png)

## Часть 2 — Подключение подопытного сервиса

За несколько итераций был сгенерирован сервис, протестирован на несколько сценариев поведения.

```yml
services:
    service:
        build: ./service
        container_name: lab3-service
        environment:
            DB_WRITE_HOSTS: pg-primary,pg-standby
            DB_READ_HOSTS: pg-standby,pg-primary
        ports:
            - "8000:8000"
        depends_on:
            - pg-primary
            - pg-standby
```

![Replication PostgreSQL сервис](./graphics/2-ui.png)

## Часть 3 — Отлов replication lag

В задании к лабораторной, чтобы увидеть lag предлагается:

> Нажми «Налить нагрузку» <...> какое-то время в списке нет только что созданных записей — реплика отстаёт.

Сначала я мерил его как разницу количества строк: `count(*)` на primary минус `count(*)` на standby. На экране получались `0` и даже `-1`. Причина в самом методе:

- Два запроса к разным серверам выполняются в разные моменты, и за это время primary успевает записать новые строки. Отрицательное значение означает не опережение standby, а то, что счётчики сняты в разные моменты.
- Задержка репликации на локальной машине меньше времени самого замера. Как и в физическом эксперименте, где время полёта фотона нельзя измерить секундомером в руке, здесь данные успевают измениться, пока мы их считаем.

После небольшого ресерча делаем вывод, отставание надо измерять не количеством записей, а позицией в журнале WAL. Позиция обновляется сразу при записи в журнал, поэтому её можно сравнивать между серверами.

- Лаг считаю как разницу двух позиций: `pg_current_wal_lsn()` на primary минус `pg_last_wal_replay_lsn()` на standby. Первая показывает, сколько журнала записано, вторая — до какого места реплика его применила. Разница через `pg_wal_lsn_diff` даёт объём в байтах, который standby ещё не применил.
- Сначала читаю standby, потом primary. Позиция реплики тогда не может опередить primary, и значение не уходит в минус. Оно может быть немного завышено, так как primary успевает записать новое между двумя запросами, но это погрешность в сторону больших значений.

В итоговой реализации сервиса получаем значения задержки (в байтах).

![Отлов replication lag](./graphics/3-ui.png)

## Часть 4 — Теорема CAP

### Асинхронный режим — доступность (Availability)

В текущей конфигурации реплика работает в асинхронном режиме, что говорит о большей приближенности к **достпуности (Availability)**. Данные могут потеряться при падении из-за гонки состояний, но при исключении реплики (`docker pause pg-standby`) система продолжает принимать записи и отдавать ответ.

![Падение реплики в асинхронном режиме](./graphics/4-async.png)

### Синхронный режим — согласованность (Consistency)

Допустим, необходимо сместиться в сторону **согласованности (Consistency)**, где исключаем потерю данных. Тогда придется переводить конфигурацию в синхронный режим, где для ответа пользователю придется дожидаться подтверждения от реплики, при этом мы теряем: в скорости ответа, в доступности записи при упавшей реплике.

Перепишем скрипт начальной инициализации реплики для работы в двух режимах ([`replica/replica-entrypoint.sh`](./config/replica/replica-entrypoint.sh)). Обновим и запуск реплики в [`docker-compose.yml`](./docker-compose.yml).

Для standby зададим имя.

```yml
pg-standby:
    environment:
        STANDBY_NAME: standby1
```

Для primary зададим изменение параметров запуска.

```yml
pg-primary:
    environment:
        # ASYNC
        # SYNC_STANDBY_NAMES: ""
        # SYNC_COMMIT: "on"
        # SYNC
        SYNC_STANDBY_NAMES: "FIRST 1 (standby1)"
        SYNC_COMMIT: "remote_apply"
```

Проверяем поведение при падении реплики (`docker pause pg-standby`) в синхронном режиме: запись не сохраняется.

![Падение реплики в синхронном режиме](./graphics/4-sync.png)

> [!NOTE]
> Для всех дальнейших экспериментов переведем связку обратно в асинхронный режим.

## Часть 5 — Failover: падение primary

### Ручной failover

Запустим стенд

![Обычное состояние](./graphics/5-1.png)

Уроним primary `docker stop pg-primary`

![Упавшая primary](./graphics/5-2.png)

Попробуем сделать запись

![Ошибка записи](./graphics/5-3.png)

Повысим standby до primary

```bash
docker exec -it pg-standby psql -U postgres -c "SELECT pg_promote();"
```

Значение `pg_is_in_recovery = f` говорит о том, что реплика в режиме primary

![Смена primary](./graphics/5-4.png)

Попробуем сделать запись снова - успех

![Запись на реплику в режиме primary](./graphics/5-5.png)

### Старый primary внезапно вернулся

Поднимем старый primary `docker start pg-primary`

![Два primary!](./graphics/5-6.png)

Сервис продолжает работать с повышенной репликой (он не стал переключаться, ведь она не падала), но начинает ругаться на `SPLIT BRAIN`. Эта ситуация опасна тем, что данные могут начать писаться на обе БД и больше не будет понятно, где источник правды.

Для предотвращения таких ситуаций нужно отдельно прописывать логику, а для устранения нарушений использовать `pg_rewind`: он откатывает расходящиеся данные старого узла до точки расхождения и докачивает недостающее с нового primary.

> [!NOTE]
> А в текущей лабе просто сносим стенд `down -v`.

### Как в проде такой ручной failover автоматизируют

Предотвращают split brain выбор лидера через etcd или Consul, кворум, fencing и watchdog.

Обычно используется Patroni: он хранит лидера в etcd или Consul с ограниченным сроком жизни, и primary продлевает его, пока работает. Если ключ истёк, standby получает его и повышается. Старый узел при возвращении понижается сам. Watchdog перезагружает узел, который не может продлить лидерство, а fencing не даёт старому primary принимать записи до повышения нового.

Альтернативы: repmgr с узлом-свидетелем, pg_auto_failover с монитором, управляемые сервисы вроде RDS и Cloud SQL. Клиентов от смены primary защищают HAProxy с проверкой роли или виртуальный IP.

## Часть 6 — Мониторинг

Подумав, погадав, полистав и почитав, приходим к следующему набору метрик:

- `up{job=~"postgres-.*"}`: доступность экспортера узла,
- `pg_up{job=~"postgres-.*"}`: доступность БД,
- `pg_replication_is_replica{job=~"postgres-.*"}`: роль узла (0 — primary, 1 — standby),
- `pg_stat_replication_pg_wal_lsn_diff{job="postgres-primary"}`: отставание standby в байтах WAL,
- `pg_stat_database_numbackends{datname="runtime_sentinel"}`: число открытых соединений с базой на каждом узле,
- `pg_stat_database_xact_commit{datname="runtime_sentinel"}`: счётчик успешных транзакций; `rate(...[1m])` даёт транзакций в секунду.

Вся конфигурация происходила аналогично предыдущим лабам и расположена в [`monitoring/`](./monitoring/) и [`docker-compose.yml`](./docker-compose.yml).

![Grafana интерфейс](./graphics/6-grafana.png)

Метрик придумалось немного и выделить по ним алерты получилось так:

- `NoPrimary` (critical). Срабатывает, когда ни один узел не отвечает в роли primary: нет узла с `up = 1` и `pg_replication_is_replica = 0`. Держится 15 секунд. Записи в кластер невозможны. Нужен промоушен standby или восстановление primary.

- `NoStandby` (critical). Срабатывает, когда у primary нет ни одной подключённой реплики в `pg_stat_replication`, при этом сам primary доступен. Держится 15 секунд. Копии данных нет: при падении primary записи будут потеряны. В синхронном режиме каждый COMMIT будет ждать реплику.

- `HighReplicationLag` (warning). Срабатывает, когда отставание standby больше 1 МБ WAL. Держится 30 секунд. Чтения с standby устаревают, а при падении primary записи из отставания потеряются.

Проверим, остановим pg-primary и увидим alert.

![Упавший pg-primary](./graphics/6-down.png)

![Алерты!](./graphics/6-alert.png)

## Выводы

В ходе работы я впервые познакомился с репликацией **PostgreSQL**. Чуть более подробно погрузился в концепции распределенного хранения, убедился, что это очень сложная задача с кучей нюансов.

Узнал о механизме репликации в Postgres через WAL и потрогал его руками. Понял, что заниматься созданием ручной отказоустойчивости без автоматики опасно, потому что возможен split brain.

Рассмотрел теорему CAP: идеальной системы хранения не существует, мы всегда чем-то жертвуем. Провел связь с асинхронным и синхронным режимом работы. Асинхронная репликация быстрее, но при падении primary можно потерять данные; синхронная надёжнее, но тормозит и виснет, пока каждая запись ждёт реплику.

[![Linux](https://img.shields.io/badge/Linux-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://www.linux.org/)
[![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
[![Docker Compose](https://img.shields.io/badge/Docker%20Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-336791?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?style=for-the-badge&logo=prometheus&logoColor=white)](https://prometheus.io/)
[![Grafana](https://img.shields.io/badge/Grafana-F46800?style=for-the-badge&logo=grafana&logoColor=white)](https://grafana.com/)
[![Alertmanager](https://img.shields.io/badge/Alertmanager-E6522C?style=for-the-badge&logo=prometheus&logoColor=white)](https://prometheus.io/docs/alerting/latest/alertmanager/)

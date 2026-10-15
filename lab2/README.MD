# Лаба 2 — Kafka для двух сервисов: подними и настрой с нуля

**Цель**: собрать простой конвейер из сервиса заказов и сервиса сборщика, соединив их брокером Kafka, настроенного и поднятого с нуля.

## Часть 1 — Написание двух сервисов

Первым делом создаем пару небольших сервисов ([`/user-service`](./user-service/) и [`/picker-service`](./picker-service/) ), наносим немного стилей и добавляем иконочки. Описываем их запуск в [`docker-compose.yml`](./docker-compose.yml).

![user-service](./graphics/1-user-service.png)

![picker-service](./graphics/1-picker-service.png)

<details>
<summary>
    Конфигурация compose для user-service и picker-service
</summary>

```yml
services:
    user-service:
        build: ./user-service
        container_name: user-service
        environment:
            KAFKA_BOOTSTRAP_SERVERS: "localhost:9092"
            KAFKA_TOPIC: "orders"
            INSTANCE_NAME: "user-service-1"
        ports:
            - "8000:8000"

    picker-service:
        build: ./picker-service
        container_name: picker-service
        environment:
            KAFKA_BOOTSTRAP_SERVERS: "localhost:9092"
            KAFKA_TOPIC: "orders"
            KAFKA_GROUP_ID: "pickers"
            INSTANCE_NAME: "picker-service-1"
            PICK_SECONDS: "2"
            AUTO_OFFSET_RESET: "earliest"
        ports:
            - "8001:8000"
```

</details>

## Часть 2 — Поднятие Kafka

Настроим в [`docker-compose.yml`](./docker-compose.yml) сервис Kafka.

> [!WARNING]
> При первом запуске на образе `confluentinc/cp-kafka` брокер упал с ошибкой `CLUSTER_ID is required`, так как для запуска необходима переменная `KAFKA_CLUSTER_ID`, которую нужно генерировать вручную.
>
> В качестве решения перейдем на `apache/kafka`, который автоматически генерирует её. Для его запуска также пришлось дополнить минимальный список переменных окружения.

Разберем используемые параметры.

| Параметр                                         | Цель                                                                                                          |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `KAFKA_NODE_ID`                                  | Уникальный ID этого брокера в кластере (для одиночного принят `1`)                                            |
| `KAFKA_PROCESS_ROLES`                            | Роли в KRaft: ранит данные (`broker`), управляет метаданными кластера (`controller`)                          |
| `KAFKA_LISTENERS`                                | Сетевые интерфейсы для связи: в сети контейнеров `29092`, для контроллера `29093` и для внешнего хоста `9092` |
| `KAFKA_LISTENER_SECURITY_PROTOCOL_MAP`           | Указывает слушателям протокол: простой незашифрованный `PLAINTEXT`                                            |
| `KAFKA_ADVERTISED_LISTENERS`                     | "Публичные" сетевые адреса: `KAFKA:29092` в сети Docker, `localhost:9092` на хост-машине                      |
| `KAFKA_CONTROLLER_LISTENER_NAMES`                | Указывает имя листенера для Kraft (`CONTROLLER`)                                                              |
| `KAFKA_CONTROLLER_QUORUM_VOTERS`                 | Список узлов-контроллеров, формирующих метаданные: `ID@хост:порт` (`1@kafka:29093`)                           |
| `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR`         | Фактор репликации для смещений (`__consumer_offsets`)                                                         |
| `KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR` | Фактор репликации для транзакций (`__transaction_state`)                                                      |
| `KAFKA_TRANSACTION_STATE_LOG_MIN_ISR`            | Минимум живых копий для записи транзакций                                                                     |
| `KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS`         | Задержка перед распределением партиций среди потребителей                                                     |

> [!NOTE]
> Отдельно рассмотрим виды слушателей: `CONTROLLER` определен для обмена метаданными KRaft, `PLAINTEXT` - подключение клиентов (используется в сети docker), `PLAINTEXT_HOST` - подключение внешних клиетов. Все трое в рамках лабораторной общаются без шифрования.

Нажми на кнопку, получишь результат.

![Минимальный запуск сервисов](./graphics/2-work.png)

<details>
<summary>
    Конфигурация compose для двух сервисов и брокера
</summary>

```yml
name: lab2

services:
    user-service:
        build: ./user-service
        container_name: user-service
        environment:
            KAFKA_BOOTSTRAP_SERVERS: "kafka:29092" #
            KAFKA_TOPIC: "orders"
            INSTANCE_NAME: "user-service-1"
        ports:
            - "8000:8000"

    picker-service:
        build: ./picker-service
        container_name: picker-service
        environment:
            KAFKA_BOOTSTRAP_SERVERS: "kafka:29092" #
            KAFKA_TOPIC: "orders"
            KAFKA_GROUP_ID: "pickers"
            INSTANCE_NAME: "picker-service-1"
            PICK_SECONDS: "2"
            AUTO_OFFSET_RESET: "earliest"
        ports:
            - "8001:8000"

    kafka:
        image: apache/kafka:latest
        container_name: kafka
        ports:
            - "9092:9092"
        environment:
            KAFKA_NODE_ID: 1
            KAFKA_PROCESS_ROLES: "broker,controller"
            KAFKA_LISTENERS: "PLAINTEXT://0.0.0.0:29092,CONTROLLER://0.0.0.0:29093,PLAINTEXT_HOST://0.0.0.0:9092"
            KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,PLAINTEXT_HOST:PLAINTEXT"
            KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka:29092,PLAINTEXT_HOST://localhost:9092"
            KAFKA_CONTROLLER_LISTENER_NAMES: "CONTROLLER"
            KAFKA_CONTROLLER_QUORUM_VOTERS: "1@kafka:29093"
            KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
            KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
            KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
            KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
        volumes:
            - kafka_data:/var/lib/kafka/data

volumes:
    kafka_data:
```

</details>

## Часть 3 — Бизнес-ситуации (решение через конфигурацию)

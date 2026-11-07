# Пример ER-диаграммы (учебный, не про проект)

## Диаграмма (Mermaid)

```mermaid
erDiagram
    CUSTOMER ||--o{ ORDER : "оформляет"
    ORDER ||--|{ ORDER_ITEM : "содержит"
    PRODUCT ||--o{ ORDER_ITEM : "входит в"
    ORDER ||--o| PAYMENT : "оплачивается"

    CUSTOMER {
        int customer_id PK
        string full_name
        string email UK
        datetime created_at
    }

    ORDER {
        int order_id PK
        int customer_id FK
        string status
        decimal total_amount
        datetime created_at
    }

    PRODUCT {
        int product_id PK
        string title
        decimal price
        int stock_qty
    }

    ORDER_ITEM {
        int order_item_id PK
        int order_id FK
        int product_id FK
        int quantity
        decimal unit_price
    }

    PAYMENT {
        int payment_id PK
        int order_id FK
        string method
        string status
        datetime paid_at
    }
```

## Как это читать

| Обозначение | Значение |
|---|---|
| `PK` | первичный ключ (primary key) |
| `FK` | внешний ключ (foreign key) |
| `UK` | уникальное значение (unique key) |
| `||--o{` | один-ко-многим: у одного заказа много позиций |
| `||--|{` | один-ко-многим, обязательная связь (минимум одна позиция) |
| `||--o|` | один-к-одному или нулю (заказ может быть не оплачен) |

## Тот же пример словами

- **CUSTOMER** — покупатель. Один покупатель может оформить много заказов.
- **ORDER** — заказ. Принадлежит одному покупателю, состоит из одной или нескольких позиций.
- **PRODUCT** — товар. Может входить во многие позиции разных заказов.
- **ORDER_ITEM** — позиция заказа (связующая таблица между заказом и товаром).
- **PAYMENT** — оплата. У заказа может быть один платёж или ни одного.

## Вариант без Mermaid (если редактор не поддерживает)

```text
CUSTOMER (customer_id PK, full_name, email UK, created_at)
   │ 1
   │
   │ N
ORDER (order_id PK, customer_id FK, status, total_amount, created_at)
   │ 1                │ 1
   │                  │
   │ N                │ 0..1
ORDER_ITEM (...)   PAYMENT (payment_id PK, order_id FK, method, status, paid_at)
   │ N
   │
   │ 1
PRODUCT (product_id PK, title, price, stock_qty)
```

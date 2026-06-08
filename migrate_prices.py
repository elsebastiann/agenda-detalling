"""
Migración de precios según Catálogo NOXA.
Ejecutar una vez en el próximo despliegue: python migrate_prices.py

Acciones:
  1. Renombra "Wash Motor" -> "Detallado Motor"
  2. Elimina servicios que empiezan por "Enjuague"
  3. Upsert de precios del catálogo para Auto, SUV, Camioneta y Moto
"""

from app import app, db, Service, VehicleType, ServicePrice

# Precios del catálogo: {nombre_servicio: {nombre_vehiculo: precio}}
CATALOG = {
    "Coating Ceramico 7H+": {
        "Automovil": 899000,
        "SUV":      1099000,
        "Camioneta":1299000,
        "Moto":      399000,
    },
    "Coating Ceramico 9H": {
        "Automovil": 1899000,
        "SUV":       2199000,
        "Camioneta": 2499000,
        "Moto":       799000,
    },
    "Wash Shine": {
        "Automovil":  65000,
        "SUV":        70000,
        "Camioneta":  85000,
        "Moto":       45000,
    },
    "Wash Essential": {
        "Automovil":  45000,
        "SUV":        50000,
        "Camioneta":  60000,
        "Moto":       35000,
    },
    "Detallado Exterior": {
        "Automovil":  90000,
        "SUV":       110000,
        "Camioneta": 150000,
        "Moto":       70000,
    },
    "Wash Chasis": {
        "Automovil":  80000,
        "SUV":        90000,
        "Camioneta": 100000,
    },
    "Detallado Motor": {
        "Automovil":  80000,
        "SUV":        90000,
        "Camioneta": 100000,
    },
    "Detallado Interior": {
        "Automovil": 270000,
        "SUV":       330000,
        "Camioneta": 410000,
    },
    "Detallado Llanta a Llanta": {
        "Automovil": 110000,
        "SUV":       110000,
        "Camioneta": 110000,
    },
    "Polichado": {
        "Automovil": 180000,
        "SUV":       230000,
        "Camioneta": 280000,
        "Moto":      120000,
    },
    "Correccion de Wrap": {
        "Automovil": 180000,
        "SUV":       230000,
        "Camioneta": 280000,
        "Moto":      120000,
    },
    "Porcelanizado": {
        "Automovil": 290000,
        "SUV":       340000,
        "Camioneta": 390000,
        "Moto":      150000,
    },
}

with app.app_context():
    # 1. Renombrar "Wash Motor" -> "Detallado Motor"
    wash_motor = Service.query.filter_by(name="Wash Motor").first()
    if wash_motor:
        wash_motor.name = "Detallado Motor"
        print("✓ Renombrado: Wash Motor -> Detallado Motor")
    else:
        print("- Wash Motor no encontrado (puede ya estar renombrado)")

    # 2. Eliminar servicios que empiezan por "Enjuague"
    enjuagues = Service.query.filter(Service.name.ilike("Enjuague%")).all()
    for s in enjuagues:
        # Eliminar primero sus precios asociados
        ServicePrice.query.filter_by(service_id=s.id).delete()
        db.session.delete(s)
        print(f"✓ Eliminado servicio: {s.name}")
    if not enjuagues:
        print("- No se encontraron servicios Enjuague")

    db.session.flush()

    # 3. Upsert de precios del catálogo
    vehicle_cache = {vt.name: vt for vt in VehicleType.query.all()}
    service_cache = {s.name: s for s in Service.query.all()}

    updated = 0
    created = 0
    skipped = 0

    for service_name, prices_by_vehicle in CATALOG.items():
        service = service_cache.get(service_name)
        if not service:
            print(f"! Servicio no encontrado en sistema: {service_name!r} — omitido")
            skipped += 1
            continue

        for vehicle_name, price in prices_by_vehicle.items():
            vehicle = vehicle_cache.get(vehicle_name)
            if not vehicle:
                print(f"! Tipo de vehículo no encontrado: {vehicle_name!r} — omitido")
                skipped += 1
                continue

            sp = ServicePrice.query.filter_by(
                service_id=service.id,
                vehicle_type_id=vehicle.id
            ).first()

            if sp:
                sp.price = price
                sp.is_active = True
                updated += 1
            else:
                sp = ServicePrice(
                    service_id=service.id,
                    vehicle_type_id=vehicle.id,
                    price=price,
                    duration_minutes=60,
                    is_active=True,
                )
                db.session.add(sp)
                created += 1

    db.session.commit()
    print(f"\nPrecios: {updated} actualizados, {created} creados, {skipped} omitidos.")
    print("Migración completada.")

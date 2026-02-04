"""
Tests for nested device functionality (multi-level device nesting).

Tests cover:
- Device creation at multiple nesting levels
- Recursion depth limit enforcement
- Infinite loop detection and prevention
- u_height validation for nested devices
- Location and rack auto-correction cascading
- All subdevice role combinations (PARENT, CHILD, PARENT_CHILD)
- Query methods for nested hierarchies
"""

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase

from nautobot.core.utils.cache import construct_cache_key
from nautobot.dcim.choices import DeviceFaceChoices, SubdeviceRoleChoices
from nautobot.dcim.constants import DEVICE_RECURSION_DEPTH_LIMIT
from nautobot.dcim.models import (
    Device,
    DeviceBay,
    DeviceBayTemplate,
    DeviceType,
    Location,
    LocationType,
    Manufacturer,
    Rack,
)
from nautobot.extras.models import Role, Status


class NestedDeviceTestBase(TestCase):
    """Base test class with common setup for nested device tests."""

    @classmethod
    def setUpTestData(cls):
        """Create shared test data."""
        print(f"\nSetting up test data for {cls.__name__}...")  # Debug statement to indicate setup start
        # Status
        cls.status = Status.objects.get_for_model(Device).first()

        # Location
        cls.location_status = Status.objects.get_for_model(Location).first()
        cls.location_type = LocationType.objects.create(name="Test Location Type")
        cls.location_type.content_types.add(ContentType.objects.get_for_model(Device))
        cls.location, _ = Location.objects.get_or_create(
            name="Test Location",
            defaults={"location_type": cls.location_type, "status": cls.location_status},
        )
        if cls.location.location_type_id is None or cls.location.status_id is None:
            cls.location.location_type = cls.location_type
            cls.location.status = cls.location_status
            cls.location.save()

        # Rack
        cls.rack_status = Status.objects.get_for_model(Rack).first()
        cls.rack = Rack.objects.create(name="Test Rack", location=cls.location, u_height=42, status=cls.rack_status)

        # Manufacturer
        cls.manufacturer = Manufacturer.objects.create(name="Test Manufacturer")

        # Role
        cls.role = Role.objects.get_for_model(Device).first()

        # Device Types
        cls.parent_device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer,
            model="Parent Device",
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
            u_height=4,
        )

        cls.parent_child_device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer,
            model="Parent-Child Device",
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT_CHILD,
            u_height=0,
        )

        cls.child_device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer,
            model="Child Device",
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
            u_height=0,
        )

    def create_device_bay_template(self, device_type, bay_name="Bay1"):
        """Helper to create device bay template."""
        return DeviceBayTemplate.objects.create(device_type=device_type, name=bay_name)

    def create_device_bay(self, device, name="Bay1"):
        """Helper to create device bay with cache invalidation."""
        device_bay = DeviceBay.objects.create(device=device, name=name)
        self.clear_has_device_bays_cache(device)
        return device_bay

    def clear_has_device_bays_cache(self, device):
        """Clear cached has_device_bays value for a device."""
        cache.delete(construct_cache_key(device, method_name="has_device_bays", branch_aware=True))

    def create_device(self, name, device_type, location=None, rack=None, position=None, parent_bay=None):
        """Helper to create device with standard parameters."""
        is_child_device = device_type.is_child_device
        if parent_bay is not None:
            location = location or parent_bay.device.location
            rack = rack or parent_bay.device.rack

        location = location or self.location
        face = DeviceFaceChoices.FACE_FRONT if rack and not is_child_device else ""

        device = Device.objects.create(
            name=name,
            device_type=device_type,
            location=location,
            rack=rack,
            position=position,
            face=face,
            status=self.status,
            role=self.role,
        )

        if parent_bay is not None:
            parent_bay.installed_device = device
            parent_bay.validated_save()
            self.clear_has_device_bays_cache(parent_bay.device)
            device.refresh_from_db()
        return device


class NestedDeviceCreationTests(NestedDeviceTestBase):
    """Tests for creating nested device hierarchies."""

    def test_create_parent_device(self):
        """Test creating a basic parent device."""
        device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        self.assertEqual(device.name, "parent1")
        self.assertEqual(device.device_type.subdevice_role, SubdeviceRoleChoices.ROLE_PARENT)
        self.assertTrue(device.device_type.is_parent_device)

    def test_create_parent_child_device(self):
        """Test creating a parent-child device (can be both parent and child)."""
        device = self.create_device("parentchild1", self.parent_child_device_type)
        self.assertEqual(device.device_type.subdevice_role, SubdeviceRoleChoices.ROLE_PARENT_CHILD)
        self.assertTrue(device.device_type.is_parent_device)

    def test_create_two_level_hierarchy(self):
        """Test creating a two-level hierarchy: Parent → Child."""
        # Create parent device
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)

        # Create device bay on parent
        parent_device_bay = self.create_device_bay(parent_device, "Bay1")

        # Create child device in parent bay
        child_device = self.create_device("child1", self.child_device_type, parent_bay=parent_device_bay)

        parent_device.save()
        child_device.refresh_from_db()

        # Verify relationships
        self.assertEqual(parent_device_bay, child_device.parent_bay)
        self.assertIsNone(child_device.position)
        self.assertEqual(parent_device.location, child_device.location)
        self.assertEqual(parent_device.rack, child_device.rack)

    def test_create_three_level_hierarchy(self):
        """Test creating a three-level hierarchy: Parent → Parent-Child → Child."""
        # Level 1: Parent
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        parent_device_bay = self.create_device_bay(parent_device, "Bay1")

        # Level 2: Parent-Child
        parent_child_device = self.create_device(
            "parentchild1", self.parent_child_device_type, parent_bay=parent_device_bay
        )
        # Create device bay on parent-child
        parent_child_device_bay = self.create_device_bay(parent_child_device, "BayA")

        # Level 3: Child
        child_device = self.create_device("child1", self.child_device_type, parent_bay=parent_child_device_bay)

        parent_device.save()
        parent_child_device.refresh_from_db()
        child_device.refresh_from_db()

        # Verify relationships
        self.assertEqual(parent_device_bay, parent_child_device.parent_bay)
        self.assertEqual(parent_child_device_bay, child_device.parent_bay)
        self.assertIsNone(parent_child_device.position)
        self.assertIsNone(child_device.position)
        self.assertEqual(parent_device.location, parent_child_device.location)
        self.assertEqual(parent_device.location, child_device.location)
        self.assertEqual(parent_device.rack, parent_child_device.rack)
        self.assertEqual(parent_device.rack, child_device.rack)

    def test_create_four_level_hierarchy(self):
        """Test creating maximum nesting depth (4 levels)."""
        # Level 1
        level1_device = self.create_device("level1", self.parent_device_type, rack=self.rack, position=1)
        level1_device_bay = self.create_device_bay(level1_device, "Bay1")

        # Level 2
        level2_device = self.create_device("level2", self.parent_child_device_type, parent_bay=level1_device_bay)
        level2_device_bay = self.create_device_bay(level2_device, "Bay2")

        # Level 3
        level3_device = self.create_device("level3", self.parent_child_device_type, parent_bay=level2_device_bay)
        level3_device_bay = self.create_device_bay(level3_device, "Bay3")

        # Level 4
        level4 = self.create_device("level4", self.child_device_type, parent_bay=level3_device_bay)

        level1_device.save()
        level2_device.refresh_from_db()
        level3_device.refresh_from_db()
        level4.refresh_from_db()

        # Verify complete chain
        self.assertEqual(level3_device_bay, level4.parent_bay)
        self.assertEqual(level2_device_bay, level3_device.parent_bay)
        self.assertEqual(level1_device_bay, level2_device.parent_bay)
        self.assertIsNone(level2_device.position)
        self.assertIsNone(level3_device.position)
        self.assertIsNone(level4.position)
        self.assertEqual(level1_device.location, level2_device.location)
        self.assertEqual(level1_device.location, level3_device.location)
        self.assertEqual(level1_device.location, level4.location)
        self.assertEqual(level1_device.rack, level2_device.rack)
        self.assertEqual(level1_device.rack, level3_device.rack)
        self.assertEqual(level1_device.rack, level4.rack)

    def test_exceed_recursion_depth_limit(self):
        """Test that all_nested_devices is limited by recursion depth."""
        # Build a hierarchy that would exceed the limit
        root_device = self.create_device("level1", self.parent_device_type, rack=self.rack, position=1)
        current_device = root_device

        # Create DEVICE_RECURSION_DEPTH_LIMIT levels
        for i in range(2, DEVICE_RECURSION_DEPTH_LIMIT + 2):
            bay = self.create_device_bay(current_device, f"Bay{i}")
            device_type = (
                self.parent_child_device_type if i < DEVICE_RECURSION_DEPTH_LIMIT + 1 else self.child_device_type
            )
            current_device = self.create_device(f"level{i}", device_type, parent_bay=bay)

        # all_nested_devices should be limited to DEVICE_RECURSION_DEPTH_LIMIT
        self.assertEqual(root_device.all_nested_devices.count(), DEVICE_RECURSION_DEPTH_LIMIT)

    def test_cannot_create_child_device_with_nonzero_u_height(self):
        """Test that creating child device type with u_height > 0 fails."""
        for role in (SubdeviceRoleChoices.ROLE_CHILD, SubdeviceRoleChoices.ROLE_PARENT_CHILD):
            with self.subTest(role=role):
                with self.assertRaises(ValidationError):
                    DeviceType.objects.create(
                        manufacturer=self.manufacturer,
                        model=f"Invalid {role}",
                        subdevice_role=role,
                        u_height=1,  # Invalid: child devices must be 0U
                    ).full_clean()


class InfiniteLoopPreventionTests(NestedDeviceTestBase):
    """Tests for infinite loop detection and prevention."""

    def test_cannot_create_self_referential_parent_bay(self):
        """Test that a device cannot be its own parent."""
        device = self.create_device("device1", self.parent_child_device_type)
        device_bay = self.create_device_bay(device, "Bay1")

        # Try to set device as its own parent
        device.parent_bay = device_bay
        with self.assertRaises(ValidationError):
            device.save()

    def test_cannot_create_circular_reference(self):
        """Test that circular parent-child references are prevented."""
        # Create two devices
        device1 = self.create_device("device1", self.parent_child_device_type)
        device1_bay = self.create_device_bay(device1, "Bay1")

        device2 = self.create_device("device2", self.parent_child_device_type, parent_bay=device1_bay)
        device2_bay = self.create_device_bay(device2, "Bay2")

        # Try to make device1 a child of device2 (would create circle)
        device1.parent_bay = device2_bay
        with self.assertRaises(ValidationError):
            device1.save()


class LocationAndRackAutoCorrectionTests(NestedDeviceTestBase):
    """Tests for automatic location and rack inheritance/correction."""

    def test_location_change_cascades_to_children(self):
        """Test that changing parent location cascades to all children."""
        # Create parent and child
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        parent_device_bay = self.create_device_bay(parent_device, "Bay1")
        child = self.create_device("child1", self.child_device_type, parent_bay=parent_device_bay)

        # Verify child has parent's location
        self.assertEqual(parent_device.location, child.location)

        # Create new location and move parent
        new_location = Location.objects.create(
            name="New Location", location_type=self.location_type, status=self.location_status
        )
        parent_device.location = new_location
        parent_device.save()

        # Refresh child and verify location changed
        child.refresh_from_db()

        self.assertEqual(child.location, new_location)

    def test_rack_change_cascades_to_children(self):
        """Test that changing parent rack cascades to all children."""
        parent_device = self.create_device(
            "parent1", self.parent_device_type, location=self.location, rack=self.rack, position=1
        )
        parent_device_bay = self.create_device_bay(parent_device, "Bay1")
        child_device = self.create_device(
            "child1",
            self.child_device_type,
            location=parent_device.location,
            rack=parent_device.rack,
            parent_bay=parent_device_bay,
        )

        # Create new rack and move parent
        new_rack = Rack.objects.create(name="New Rack", location=self.location, u_height=42, status=self.rack_status)
        parent_device.rack = new_rack
        parent_device.position = 2
        parent_device.save()

        # Verify child's rack changed
        child_device.refresh_from_db()
        self.assertEqual(child_device.rack, new_rack)

    def test_multi_level_cascade(self):
        """Test that auto-correction cascades through multiple nesting levels."""
        # Build 3-level hierarchy
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        parent_device_bay = self.create_device_bay(parent_device, "Bay1")

        parent_child = self.create_device("parentchild1", self.parent_child_device_type, parent_bay=parent_device_bay)
        parent_child_bay = self.create_device_bay(parent_child, "Bay2")

        child = self.create_device("child1", self.child_device_type, parent_bay=parent_child_bay)

        parent_device.validated_save()
        parent_child.refresh_from_db()
        child.refresh_from_db()

        # Verify all have same location/rack
        self.assertEqual(parent_device.location, parent_child.location)
        self.assertEqual(parent_child.location, child.location)
        self.assertEqual(parent_device.rack, parent_child.rack)
        self.assertEqual(parent_child.rack, child.rack)

        # Change parent location
        new_location = Location.objects.create(
            name="New Location", location_type=self.location_type, status=self.location_status
        )
        parent_device.location = new_location
        parent_device.save()

        # Verify change cascaded to all descendants
        parent_child.refresh_from_db()
        child.refresh_from_db()

        self.assertEqual(parent_child.location, new_location)
        self.assertEqual(child.location, new_location)


class DeviceQueryMethodsTests(NestedDeviceTestBase):
    """Tests for device query methods supporting nested hierarchies."""

    def test_has_device_bays_property(self):
        """Test has_device_bays property on device types."""
        # Ensure the parent device type has at least one bay template so bays are created on save.
        self.create_device_bay_template(self.parent_device_type)
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        child_device = self.create_device("child1", self.child_device_type)

        # Clear cached value after bay creation to avoid stale results.
        self.clear_has_device_bays_cache(parent_device)

        self.assertTrue(parent_device.has_device_bays)
        self.assertFalse(child_device.has_device_bays)

    def test_all_nested_devices_single_level(self):
        """Test all_nested_devices query with single-level children."""
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        parent_device_bay1 = self.create_device_bay(parent_device, "Bay1")
        parent_device_bay2 = self.create_device_bay(parent_device, "Bay2")

        child_device1 = self.create_device("child1", self.child_device_type, parent_bay=parent_device_bay1)
        child_device2 = self.create_device("child2", self.child_device_type, parent_bay=parent_device_bay2)

        nested = parent_device.all_nested_devices
        self.assertEqual(nested.count(), 2)
        self.assertIn(child_device1, nested)
        self.assertIn(child_device2, nested)

    def test_all_nested_devices_multi_level(self):
        """Test all_nested_devices query with multi-level nesting."""
        # Level 1
        parent_device = self.create_device("parent1", self.parent_device_type, rack=self.rack, position=1)
        parent_device_bay = self.create_device_bay(parent_device, "Bay1")

        # Level 2
        parent_child_device = self.create_device(
            "parentchild1", self.parent_child_device_type, parent_bay=parent_device_bay
        )
        parent_child_device_bay = self.create_device_bay(parent_child_device, "Bay2")

        # Level 3
        child_device = self.create_device("child1", self.child_device_type, parent_bay=parent_child_device_bay)

        # Query should return both level 2 and level 3 devices
        nested_devices = parent_device.all_nested_devices
        self.assertEqual(nested_devices.count(), 2)
        self.assertIn(parent_child_device, nested_devices)
        self.assertIn(child_device, nested_devices)

    def test_is_parent_device(self):
        """Test is_parent_device property on device types."""
        self.assertTrue(self.parent_device_type.is_parent_device)
        self.assertTrue(self.parent_child_device_type.is_parent_device)
        self.assertFalse(self.child_device_type.is_parent_device)

"""Test poses for the retopo checks (exec'd by render_views.py and metrics.py, in background Blender on scratch
copies only). Bones come from avatar_config.py's BONES roles and the axes from its FRONT, so they work on any
humanoid rig with Z up; the caller puts the config in the namespace as CFG.
"""
import math

from mathutils import Matrix, Vector

POSES = ("rest", "armsup", "bent", "legbend", "fist")


def rotate_bone(arm, vl, name, axis, deg):
    """Rotates a pose bone about a world-space axis through its current head."""
    pb = arm.pose.bones[name]
    vl.update()
    head = arm.matrix_world @ pb.head
    r = Matrix.Rotation(math.radians(deg), 4, Vector(axis))
    world = Matrix.Translation(head) @ r @ Matrix.Translation(-head) @ arm.matrix_world @ pb.matrix
    pb.matrix = arm.matrix_world.inverted() @ world
    vl.update()


def set_pose(arm, vl, pose):
    bones = CFG["BONES"]
    up = Vector((0, 0, 1))
    front = Vector(CFG["FRONT"]).normalized()
    left = up.cross(front)  # the avatar's own left
    back = -front

    def rot(role, axis, deg):
        rotate_bone(arm, vl, bones[role], axis, deg)
    for pb in arm.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    vl.update()
    if pose == "armsup":  # arms raised 70 degrees above the T-pose, slightly forward
        rot("upper_arm_l", back, -70)
        rot("upper_arm_r", back, 70)
        rot("upper_arm_l", up, 15)
        rot("upper_arm_r", up, -15)
    elif pose == "bent":  # arms down and forward, elbows bent, torso bent forward and twisted
        rot("spine", left, 15)
        rot("chest", left, 10)
        rot("chest", up, 15)
        rot("upper_arm_l", back, 60)
        rot("upper_arm_r", back, -60)
        rot("upper_arm_l", up, 30)
        rot("upper_arm_r", up, -30)
        rot("lower_arm_l", up, 80)
        rot("lower_arm_r", up, -80)
    elif pose == "legbend":  # left hip flexed 80 degrees, knee bent 110, toes pointed (a deep sit)
        rot("upper_leg_l", left, -80)
        rot("lower_leg_l", left, 110)
        rot("foot_l", left, -25)
    elif pose == "fist":  # left fingers curled toward the palm (thumb left alone), wrist bent down 20
        rot("hand_l", back, 20)
        for chain in bones.get("fingers_l", []):
            for name, deg in zip(chain, (70, 90, 60)):
                rotate_bone(arm, vl, name, back, deg)

import math
import time
import threading
from typing import Any, Tuple, Union

import rclpy
from rclpy.logging import LoggingSeverity
from builtin_interfaces.msg import Duration
import numpy as np
import modern_robotics as mr

from interbotix_xs_msgs.msg import *
from interbotix_xs_msgs.srv import RobotInfo, RegisterValues
from interbotix_common_modules import angle_manipulation as ang
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from interbotix_xs_modules import mr_descriptions as mrd
from interbotix_xs_modules.core import InterbotixRobotXSCore
from interbotix_xs_modules.gripper import InterbotixGripperXSInterface


class InterbotixManipulatorXS(object):
    """Standalone Module to control an Interbotix Arm and Gripper"""

    def __init__(
        self,
        robot_model: str,
        group_name: str = "arm",
        gripper_name: str = "gripper",
        robot_name: float = None,
        moving_time: float = 2.0,
        accel_time: float = 0.3,
        gripper_pressure: float = 0.5,
        gripper_pressure_lower_limit: int = 150,
        gripper_pressure_upper_limit: int = 350,
        init_node: bool = True,
        logging_level: LoggingSeverity.INFO = LoggingSeverity.INFO,
    ) -> None:
        """Definition of the Standalone Interbotix Manipulator Module

        :param robot_model: Interbotix Arm model (ex. 'wx200' or 'vx300s')
        :param group_name: (optional) joint group name that contains the 'arm' joints as
            defined in the 'motor_config' yaml file; typically, this is 'arm'
        :param gripper_name: (optional) name of the gripper joint as defined in the
            'motor_config' yaml file; typically, this is 'gripper'
        :param robot_name: (optional) defaults to value given to `robot_model`; this can
            be customized if controlling two of the same arms from one computer (like
            'arm1/wx200' and 'arm2/wx200')
        :param moving_time: (optional) time [s] it should take for all joints in the arm
            to complete one move
        :param accel_time: (optional) time [s] it should take for all joints in the arm
            to accelerate/decelerate to/from max speed
        :param use_gripper: (optional) `True` if the gripper module should be
            initialized; otherwise, it won't be.
        :param gripper_pressure: (optional) fraction from 0 - 1 where '0' means the
            gripper operates at `gripper_pressure_lower_limit` and '1' means the gripper
            operates at `gripper_pressure_upper_limit`
        :param gripper_pressure_lower_limit: (optional) lowest 'effort' that should be
            applied to the gripper if `gripper_pressure` is set to 0; it should be high
            enough to open/close the gripper (~150 PWM or ~400 mA current)
        :param gripper_pressure_upper_limit: (optional) largest 'effort' that should be
            applied to the gripper if `gripper_pressure` is set to 1; it should be low
            enough that the motor doesn't 'overload' when gripping an object for a few
            seconds (~350 PWM or ~900 mA)
        :param init_node: (optional) set to `True` if the `InterbotixRobotXSCore` class
            should initialize the ROS node - this is the most Pythonic approach; to
            incorporate a robot into an existing ROS node though, set to `False`
        """
        self.dxl = InterbotixRobotXSCore(
            robot_model=robot_model,
            robot_name=robot_name,
            init_node=init_node,
            logging_level=logging_level,
        )
        self.arm = InterbotixArmXSInterface(
            core=self.dxl,
            robot_model=robot_model,
            group_name=group_name,
            moving_time=moving_time,
            accel_time=accel_time,
        )
        if gripper_name is not None:
            self.gripper = InterbotixGripperXSInterface(
                core=self.dxl,
                gripper_name=gripper_name,
                gripper_pressure=gripper_pressure,
                gripper_pressure_lower_limit=gripper_pressure_lower_limit,
                gripper_pressure_upper_limit=gripper_pressure_upper_limit,
            )

        rclpy.spin_once(self.dxl)
        self.dxl.initialize()
        rclpy.spin_once(self.dxl)
        self.arm.initialize()
        if self.gripper is not None:
            self.gripper.initialize()
        # TODO: is this the best way to do this?
        self._execution_thread = threading.Thread(target=rclpy.spin, args=(self.dxl,))
        self._execution_thread.start()

    def shutdown(self):
        """Destroys the node and shuts down all threads and processes"""
        print("Destroying InterbotixManipulatorXS...")
        self.dxl.destroy_node()
        rclpy.shutdown()
        time.sleep(0.5)
        self._execution_thread.join(timeout=5.0)
        if self._execution_thread.is_alive():
            print("Taking a long time to destroy. Press Ctrl+C twice to exit.")
        print("Destroyed InterbotixManipulatorXS!")


class InterbotixArmXSInterface:
    """Definition of the Interbotix Arm Module"""

    def __init__(
        self,
        core: InterbotixRobotXSCore,
        robot_model: str,
        group_name: str,
        moving_time: float = 2.0,
        accel_time: float = 0.3,
    ) -> None:
        """Initialize the InterbotixArmXSInterface object

        :param core: reference to the InterbotixRobotXSCore class containing the
            internal ROS plumbing that drives the Python API
        :param robot_model: Interbotix Arm model (ex. 'wx200' or 'vx300s')
        :param group_name: joint group name that contains the 'arm' joints as defined in
            the 'motor_config' yaml file; typically, this is 'arm'
        :param moving_time: (optional) time [s] it should take for all joints in the arm
            to complete one move
        :param accel_time: (optional) time [s] it should take for all joints in the arm
            to accelerate/decelerate to/from max speed
        """

        self.core = core
        self.robot_model = robot_model
        self.moving_time, self.accel_time = moving_time, accel_time
        self.group_name = group_name

        self.robot_des = getattr(mrd, self.robot_model)

        self.future_group_info = self.core.srv_get_info.call_async(
            RobotInfo.Request(cmd_type="group", name=group_name)
        )

    def initialize(self) -> None:
        """Initialize the InterbotixArmXSInterface object"""
        while rclpy.ok() and not self.future_group_info.done():
            rclpy.spin_until_future_complete(self.core, self.future_group_info)
            rclpy.spin_once(self.core)

        self.group_info: RobotInfo = self.future_group_info.result()
        if self.group_info.profile_type != "time":
            self.core.get_logger().error(
                "Please set the group's 'profile type' to 'time'."
            )
        if self.group_info.mode != "position":
            self.core.get_logger().error(
                "Please set the group's 'operating mode' to 'position'."
            )

        self.initial_guesses = [[0.0] * self.group_info.num_joints for _ in range(3)]
        self.initial_guesses[1][0] = np.deg2rad(-120)
        self.initial_guesses[2][0] = np.deg2rad(120)
        self.joint_commands = []
        self.rev = 2 * math.pi

        # update joint_commands with the present joint positions
        for name in self.group_info.joint_names:
            self.joint_commands.append(
                self.core.joint_states.position[self.core.js_index_map[name]]
            )
        # get the initial transform between the space and body frames
        self.update_Tsb()
        self.set_trajectory_time(self.moving_time, self.accel_time)

        # build the info index map between joint names and their index
        self.info_index_map = dict(
            zip(self.group_info.joint_names, range(self.group_info.num_joints))
        )

        print(
            (
                f"Arm Group Name: {self.group_name}\n"
                f"Moving Time: {self.moving_time:.2f} seconds\n"
                f"Acceleration Time: {self.accel_time:.2f} seconds\n"
                f"Drive Mode: Time-Based-Profile"
            )
        )
        print("Initialized InterbotixArmXSInterface!\n")

    def publish_positions(
        self,
        positions,
        moving_time: float = None,
        accel_time: float = None,
        blocking: bool = True,
    ) -> None:
        """Helper function to publish joint positions and block if necessary

        :param positions: desired joint positions
        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
            to the user until the robot finishes moving
        """
        self.core.get_logger().debug(f"Publishing positions={positions}")
        self.set_trajectory_time(moving_time, accel_time)
        self.joint_commands = list(positions)
        joint_commands = JointGroupCommand(
            name=self.group_name, cmd=self.joint_commands
        )
        self.core.pub_group.publish(joint_commands)
        if blocking:
            time.sleep(
                self.moving_time
            )  # TODO: once released, use rclpy.clock().sleep_for()
        self.update_Tsb()

    def set_trajectory_time(self, moving_time: float = None, accel_time: float = None):
        """Helper function to command the 'Profile_Velocity' and
        'Profile_Acceleration' motor registers

        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        """
        self.core.get_logger().debug(
            f"Updating timing params: moving_time={moving_time}, accel_time={accel_time}"
        )
        if moving_time != None and moving_time != self.moving_time:
            self.moving_time = moving_time
            self.core.srv_set_reg.call(
                RegisterValues.Request(
                    cmd_type="group",
                    name=self.group_name,
                    reg="Profile_Velocity",
                    value=int(moving_time * 1000),
                )
            )
        if accel_time != None and accel_time != self.accel_time:
            self.accel_time = accel_time
            self.core.srv_set_reg.call(
                RegisterValues.Request(
                    cmd_type="group",
                    name=self.group_name,
                    reg="Profile_Acceleration",
                    value=int(accel_time * 1000),
                )
            )

    def check_joint_limits(self, positions) -> bool:
        """Helper function to check to make sure the desired arm group's joint
        positions are all within their respective joint limits

        :param positions: the positions [rad] to check
        :return: `True` if all positions are within limits; `False` otherwise
        """
        self.core.get_logger().debug(f"Checking joint limits for positions={positions}")
        theta_list = [int(elem * 1000) / 1000.0 for elem in positions]
        speed_list = [
            abs(goal - current) / float(self.moving_time)
            for goal, current in zip(theta_list, self.joint_commands)
        ]
        # check position and velocity limits
        for x in range(self.group_info.num_joints):
            if not (
                self.group_info.joint_lower_limits[x]
                <= theta_list[x]
                <= self.group_info.joint_upper_limits[x]
            ):
                return False
            if speed_list[x] > self.group_info.joint_velocity_limits[x]:
                return False
        return True

    def check_single_joint_limit(self, joint_name, position) -> bool:
        """Helper function to check to make sure a desired position for a given
        joint is within its limits

        :param joint_name: desired joint name
        :param position: desired joint position [rad]
        :return: `True` if within limits; `False` otherwise
        """
        self.core.get_logger().debug(
            f"Checking joint {joint_name} limits for position={position}"
        )
        theta = int(position * 1000) / 1000.0
        speed = abs(
            theta - self.joint_commands[self.info_index_map[joint_name]]
        ) / float(self.moving_time)
        ll = self.group_info.joint_lower_limits[self.info_index_map[joint_name]]
        ul = self.group_info.joint_upper_limits[self.info_index_map[joint_name]]
        vl = self.group_info.joint_velocity_limits[self.info_index_map[joint_name]]
        if not (ll <= theta <= ul):
            return False
        if speed > vl:
            return False
        return True

    def set_joint_positions(
        self,
        joint_positions,
        moving_time: float = None,
        accel_time: float = None,
        blocking: bool = True,
    ) -> bool:
        """Command positions to the arm joints

        :param joint_positions: desired joint positions [rad]
        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
            to the user until the robot finishes moving
        :return: `True` if position was commanded; `False` if it wasn't due to being
            outside limits
        """
        self.core.get_logger().debug(f"setting joint_positions={joint_positions}")
        if self.check_joint_limits(joint_positions):
            self.publish_positions(joint_positions, moving_time, accel_time, blocking)
            return True
        else:
            return False

    def go_to_home_pose(
        self, moving_time: float = None, accel_time: float = None, blocking: bool = True
    ) -> None:
        """Command the arm to go to its Home pose

        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
            to the user until the robot finishes moving
        """
        self.core.get_logger().debug(f"Going to home pose")
        self.publish_positions(
            [0] * self.group_info.num_joints, moving_time, accel_time, blocking
        )

    def go_to_sleep_pose(
        self, moving_time: float = None, accel_time: float = None, blocking: bool = True
    ) -> None:
        """Command the arm to go to its Sleep pose

        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
            to the user until the robot finishes moving
        """
        self.core.get_logger().debug(f"Going to sleep pose")
        self.publish_positions(
            self.group_info.joint_sleep_positions, moving_time, accel_time, blocking
        )

    def set_single_joint_position(
        self,
        joint_name,
        position,
        moving_time: float = None,
        accel_time: float = None,
        blocking: bool = True,
    ) -> bool:
        """Command a single joint to a desired position

        :param joint_name: name of the joint to control
        :param position: desired position [rad]
        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
              to the user until the robot finishes moving
        :return: `True` if single joint was set; `False` otherwise
        :details: Note that if a moving_time or accel_time is specified, the changes
            affect ALL the arm joints, not just the specified one
        """
        self.core.get_logger().debug(
            f"Setting joint {joint_name} to position={position}"
        )
        if not self.check_single_joint_limit(joint_name, position):
            return False
        self.set_trajectory_time(moving_time, accel_time)
        self.joint_commands[self.core.js_index_map[joint_name]] = position
        single_command = JointSingleCommand(name=joint_name, cmd=position)
        self.core.pub_single.publish(single_command)
        if blocking:
            time.sleep(self.moving_time)
        self.update_Tsb()
        return True

    def set_ee_pose_matrix(
        self,
        T_sd,
        custom_guess: list = None,
        execute: bool = True,
        moving_time: float = None,
        accel_time: float = None,
        blocking: bool = True,
    ) -> Tuple[Union[np.ndarray, Any, list], bool]:
        """Command a desired end-effector pose

        :param T_sd: 4x4 Transformation Matrix representing the transform from the
            /<robot_name>/base_link frame to the /<robot_name>/ee_gripper_link frame
        :param custom_guess: (optional) list of joint positions with which to seed the
            IK solver
        :param execute: (optional) if `True`, this moves the physical robot after
            planning; otherwise, only planning is done
        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
            to the user until the robot finishes moving
        :return: joint values needed to get the end-effector to the desired pose
        :return: `True` if a valid solution was found; `False` otherwise
        """
        self.core.get_logger().debug(f"Setting ee_pose to matrix=\n{T_sd}")
        if custom_guess is None:
            initial_guesses = self.initial_guesses
        else:
            initial_guesses = [custom_guess]

        for guess in initial_guesses:
            theta_list, success = mr.IKinSpace(
                Slist=self.robot_des.Slist,
                M=self.robot_des.M,
                T=T_sd,
                thetalist0=guess,
                eomg=0.001,
                ev=0.001,
            )
            solution_found = True

            # Check to make sure a solution was found and that no joint limits were violated
            if success:
                for x in range(len(theta_list)):
                    if theta_list[x] <= -self.rev:
                        theta_list[x] %= -self.rev
                    elif theta_list[x] >= self.rev:
                        theta_list[x] %= self.rev

                    if round(theta_list[x], 3) < round(
                        self.group_info.joint_lower_limits[x], 3
                    ):
                        theta_list[x] += self.rev
                    elif round(theta_list[x], 3) > round(
                        self.group_info.joint_upper_limits[x], 3
                    ):
                        theta_list[x] -= self.rev
                solution_found = self.check_joint_limits(theta_list)
            else:
                solution_found = False

            if solution_found:
                if execute:
                    self.publish_positions(
                        theta_list, moving_time, accel_time, blocking
                    )
                    self.T_sb = T_sd
                return theta_list, True

        self.core.get_logger().warn("No valid pose could be found. Will not execute")
        return theta_list, False

    def set_ee_pose_components(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        roll: float = 0,
        pitch: float = 0,
        yaw: float = None,
        custom_guess: list = None,
        execute: bool = True,
        moving_time: float = None,
        accel_time: float = None,
        blocking: bool = True,
    ) -> Tuple[Union[np.ndarray, Any, list], bool]:
        """Command a desired end-effector pose w.r.t. the Space frame

        :param x: (optional) linear position along the X-axis of the Space frame [m]
        :param y: (optional) linear position along the Y-axis of the Space frame [m]
        :param z: (optional) linear position along the Z-axis of the Space frame [m]
        :param roll: (optional) angular position around the X-axis of the Space frame
            [rad]
        :param pitch: (optional) angular position around the Y-axis of the Space frame
            [rad]
        :param yaw: (optional) angular position around the Z-axis of the Space frame
            [rad]
        :param custom_guess: (optional) list of joint positions with which to seed the
            IK solver
        :param execute: (optional) if `True`, this moves the physical robot after
            planning; otherwise, only planning is done
        :param moving_time: (optional) duration in seconds that the robot should move
        :param accel_time: (optional) duration in seconds that that robot should spend
            accelerating/decelerating (must be less than or equal to half the
            moving_time)
        :param blocking: (optional) whether the function should wait to return control
            to the user until the robot finishes moving
        :return: joint values needed to get the end-effector to the desired pose
        :return: True if a valid solution was found; False otherwise
        :details: Do not set 'yaw' if using an arm with fewer than 6dof
        """
        if self.group_info.num_joints < 6 or (
            self.group_info.num_joints >= 6 and yaw is None
        ):
            yaw = math.atan2(y, x)
        self.core.get_logger().debug(
            (
                f"Setting ee_pose components=\n"
                f"\tx={x}\n"
                f"\ty={y}\n"
                f"\tz={z}\n"
                f"\troll={roll}\n"
                f"\tpitch={pitch}\n"
                f"\tyaw={yaw}"
            )
        )
        T_sd = np.identity(4)
        T_sd[:3, :3] = ang.eulerAnglesToRotationMatrix([roll, pitch, yaw])
        T_sd[:3, 3] = [x, y, z]
        return self.set_ee_pose_matrix(
            T_sd, custom_guess, execute, moving_time, accel_time, blocking
        )

    def set_ee_cartesian_trajectory(
        self,
        x: float = 0,
        y: float = 0,
        z: float = 0,
        roll: float = 0,
        pitch: float = 0,
        yaw: float = 0,
        moving_time: float = None,
        wp_moving_time: float = 0.2,
        wp_accel_time: float = 0.1,
        wp_period: float = 0.05,
    ) -> bool:
        """Command a desired end-effector displacement that will follow a
        straight line path (when in 'position' control mode)

        :param x: (optional) linear displacement along the X-axis w.r.t. `T_sy` [m]
        :param y: (optional) linear displacement along the Y-axis w.r.t. `T_sy` [m]
        :param z: (optional) linear displacement along the Z-axis w.r.t. `T_sy` [m]
        :param roll: (optional) angular displacement around the X-axis w.r.t. `T_sy`
            [rad]
        :param pitch: (optional) angular displacement around the Y-axis w.r.t. `T_sy`
            [rad]
        :param yaw: (optional) angular displacement around the Z-axis w.r.t. `T_sy`
            [rad]
        :param moving_time: (optional) duration in seconds that the robot should move
        :param wp_moving_time: (optional) duration in seconds that each waypoint in the
            trajectory should move
        :param wp_accel_time: (optional) duration in seconds that each waypoint in the
            trajectory should be accelerating/decelerating (must be equal to or less
            than half of `wp_moving_time`)
        :param wp_period: (optional) duration in seconds between each waypoint
        :return: `True` if a trajectory was succesfully planned and executed; otherwise
            `False`
        :details: `T_sy` is a 4x4 transformation matrix representing the pose of a
            virtual frame w.r.t. /<robot_name>/base_link. This virtual frame has the
            exact same `x`, `y`, `z`, `roll`, and `pitch` of /<robot_name>/base_link but
            contains the `yaw` of the end-effector frame
            (/<robot_name>/ee_gripper_link).
        :details: Note that `y` and `yaw` must equal 0 if using arms with less than
            6dof.
        """
        self.core.get_logger().debug(
            (
                f"Setting ee trajectory to components=\n"
                f"\tx={x}\n"
                f"\ty={y}\n"
                f"\tz={z}\n"
                f"\troll={roll}\n"
                f"\tpitch={pitch}\n"
                f"\tyaw={yaw}"
            )
        )
        if self.group_info.num_joints < 6 and (y != 0 or yaw != 0):
            self.core.get_logger().warn(
                (
                    "Please leave the 'y' and 'yaw' fields at '0' when working with "
                    "arms that have less than 6dof."
                )
            )
            return False
        rpy = ang.rotationMatrixToEulerAngles(self.T_sb[:3, :3])
        T_sy = np.identity(4)
        T_sy[:3, :3] = ang.eulerAnglesToRotationMatrix([0.0, 0.0, rpy[2]])
        T_yb = np.dot(mr.TransInv(T_sy), self.T_sb)
        rpy[2] = 0.0
        if moving_time == None:
            moving_time = self.moving_time
        accel_time = self.accel_time
        N = int(moving_time / wp_period)
        inc = 1.0 / float(N)
        joint_traj = JointTrajectory()
        joint_positions = [float(cmd) for cmd in self.joint_commands]
        for i in range(N + 1):
            joint_traj_point = JointTrajectoryPoint()
            joint_traj_point.positions = tuple(joint_positions)
            joint_traj_point.time_from_start = Duration(
                nanosec=int(i * wp_period * rclpy.constants.S_TO_NS)
            )
            joint_traj.points.append(joint_traj_point)
            if i == N:
                break
            T_yb[:3, 3] += [inc * x, inc * y, inc * z]
            rpy[0] += inc * roll
            rpy[1] += inc * pitch
            rpy[2] += inc * yaw
            T_yb[:3, :3] = ang.eulerAnglesToRotationMatrix(rpy)
            T_sd = np.dot(T_sy, T_yb)
            theta_list, success = self.set_ee_pose_matrix(
                T_sd, joint_positions, False, blocking=False
            )
            if success:
                joint_positions = theta_list
            else:
                self.core.get_logger().warn(
                    (
                        f"{(i / float(N) * 100):.2f}% of trajectory successfully "
                        "planned. Trajectory will not be executed."
                    )
                )
                break

        if success:
            self.set_trajectory_time(wp_moving_time, wp_accel_time)
            joint_traj.joint_names = self.group_info.joint_names
            current_positions = []
            with self.core.js_mutex:
                for name in joint_traj.joint_names:
                    current_positions.append(
                        self.core.joint_states.position[self.core.js_index_map[name]]
                    )
            joint_traj.points[0].positions = current_positions
            joint_traj.header.stamp = self.core.get_clock().now().to_msg()
            self.core.pub_traj.publish(
                JointTrajectoryCommand(
                    cmd_type="group", name=self.group_name, traj=joint_traj
                )
            )
            time.sleep(moving_time + wp_moving_time)
            self.T_sb = T_sd
            self.joint_commands = joint_positions
            self.set_trajectory_time(moving_time, accel_time)

        return success

    def get_joint_commands(self) -> list:
        """Get the latest commanded joint positions

        :return: list of latest commanded joint positions [rad]
        """
        self.core.get_logger().debug("Getting latest joint commands")
        return list(self.joint_commands)

    def get_single_joint_command(self, joint_name) -> float:
        """Get the latest commanded position for a given joint

        :param joint_name: joint for which to get the position
        :return: desired position [rad]
        """
        self.core.get_logger().debug(f"Getting latest command for joint {joint_name}")
        return self.joint_commands[self.info_index_map[joint_name]]

    def get_ee_pose_command(self) -> np.ndarray:
        """Get the latest commanded end-effector pose w.r.t the Space frame

        :return <4x4 matrix> - Transformation matrix
        """
        self.core.get_logger().debug("Getting latest ee pose command")
        return np.array(self.T_sb)

    def get_ee_pose(self) -> np.ndarray:
        """Get the actual end-effector pose w.r.t the Space frame

        :return: Transformation matrix
        """
        self.core.get_logger().debug("Getting actual end-effector pose")
        joint_states = [
            self.core.joint_states.position[self.core.js_index_map[name]]
            for name in self.group_info.joint_names
        ]
        T_sb = mr.FKinSpace(self.robot_des.M, self.robot_des.Slist, joint_states)
        return T_sb

    def capture_joint_positions(self) -> None:
        """Resets self.joint_commands to be the actual positions seen by the encoders

        :details: should be used whenever joints are torqued off, right after torquing
            them on again
        """
        self.core.get_logger().debug("Capturing joint positions")
        self.joint_commands = []
        for name in self.group_info.joint_names:
            self.joint_commands.append(
                self.core.joint_states.position[self.core.js_index_map[name]]
            )
        self.update_Tsb()

    def update_Tsb(self) -> None:
        """Updates the transform between the space and body frame from the current joint
        commands
        
        """
        self.core.get_logger().debug("Updating T_sb")
        self.T_sb = mr.FKinSpace(
            self.robot_des.M, self.robot_des.Slist, self.joint_commands
        )
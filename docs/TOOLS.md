
# Tools


# cs-instance-watcher

**Purpose:** Optimize EC2 instance draining mode handling when CloneSquad is used without EC2 TargetGroup.

This daemon is to install and run on EC2 instances part of a CloneSquad group.

This scripts allows to react on instance state change (ex: `running` to `draining`).

> Without `cs-instance-watcher`, **in an EC2 target group less context and with a non-ELB Load-Balancer**, 
users could see latencies, timeouts or sharp disconnections due to abrupt draining instance shutdowns.

Even if the tool is designed to track and possibly react to any state transition, it is 
meant to react especially to the `draining` state:
When no EC2 target group is used, a possible use-case is when a load-balancer (different 
from an AWS ALB/NLB/CLB) is serving CloneSquad managed instances.

In order to help non-ELB Load-Balancer detect the draining instance condition, the
`cs-instance-watcher` daemon contains an **optional** pre-built algorithm that can reject all new TCP connections
toward a user-supplied list of ports while allowing the current established ones to continue.
The external load-balancer health-checks will thus fail as soon as the instance
falls in the `draining` state allowing a smooth redirection toward other serving
instances without users noticing the event. This option uses `iptables` so the tool needs to be run as root when used.

Users have also the option to write their own logic for reacting to `draining` (or other) state change by providing user scripts.
By default, scripts under a subdirectory of `/etc/cs-instance-watcher.d/` with the name of a state, will be executed when this state happens.

	/etc/cs-instance-watcher.d/draining/script_to_launch_on_draining_state_transition.sh


## Installation

Pre-requisites:

* If use of the builtin port blacklisting feature,
	* Collect the port(s) to blacklist on `draining` instance condition,
	* Run the tool as `root` while passing the ports to blacklist as argument,
* Install Python3 and the packages `requests`, `requests-iamauth` and `boto3`.

Python packages installation: 

	python3 -m pip install requests requests-iamauth boto3


1) Copy the tool file [here](../tools/cs-instance-watcher) to the EC2 instance.
	* The tool does not need the DevKit and only needs the packages listed in the pre-requisites.
2) Run the tool with appropriate arguments (See [Configuration](#Configuration)).

**Example:**

The following lines could be part of the EC2 Instance user-data to install once and run the tool at every start automatically.
It uses the builtin capability of the tool to generate itself the `systemd` entry to run as system daemon.

	# Download the tool
	curl https://raw.githubusercontent.com/jcjorel/clonesquad-ec2-pet-autoscaler/master/tools/cs-instance-watcher -o /usr/local/sbin/cs-instance-watcher
	chmod a+x /usr/local/sbin/cs-instance-watcher
	# Create a systemd service for the tool that will blacklit port 80 and 443 on 'draining' condition.
	/usr/local/sbin/cs-instance-watcher --on-draining-block-new-connections-to-ports 80 443 --log-file /var/log/cs-instance-watcher.log --log-file-rotate d,1,10 --generate-systemd /etc/systemd/system/cs-instance-watcher.service
	# Start the instance watcher ready to block ports 80 and 443 if the instance is placed in draining state
	systemctl enable cs-instance-watcher
	systemctl start cs-instance-watcher

> **Note: The tool can be safely left installed and started on any CloneSquad or non-CloneSquad managed EC2 instances.**


### Configuration

The tool takes command line arguments:

	usage: cs-instance-watcher [-h] [--api-polling-period [API_POLLING_PERIOD]]
	                          [--on-draining-block-new-connections-to-ports [ON_DRAINING_BLOCK_NEW_CONNECTIONS_TO_PORTS [ON_DRAINING_BLOCK_NEW_CONNECTIONS_TO_PORTS ...]]]
	                          [--config [CONFIG]] [--script-dir [SCRIPT_DIR]]
	                          [--instance-state [INSTANCE_STATE]]
	                          [--stale-context-timeout [STALE_CONTEXT_TIMEOUT]]
	                          [--force [FORCE]] [--log-file [LOG_FILE]]
	                          [--log-file-rotate [LOG_FILE_ROTATE]]
	                          [--generate-systemd [GENERATE_SYSTEMD]]
	                          [--do-not-generate-systemd [DO_NOT_GENERATE_SYSTEMD]]

**Arguments:**

* `--api-polling-period <seconds>`: Period between calls to the CloneSquad API GW to get instance status (`running`, `draining`...). Default: `5`
* `--on-draining-block-new-connections-to-ports <port> <port>`: Activate the builtin algorithm which forbids new TCP connections to the specified ports on `draining` condition. Default: `None`
* `--stale-context-timeout <seconds>`: Period of time for running instance data caching (Tags especially). Default: `30`
* `--log-file <path_to_a_file>`: Path to the rotated log file. Default: `stderr`
* `--log-file-rotate log_rotate_spec`: Log rotation specification. Format: TimeUnit,RotationPerTimeUnit,BackupFileCount. Default: `h,1,24`
	* Default `h,1,24` means: Every hour rotates logs and keep 24 hours of rotated files.
* `--generate-systemd <systemd_service_file>`: Path to a systemd service configuration file to create.
* `--script-dir <directory>`: A directory containing scripts to launch on state change. Default: /etc/cs-instance-watcher.d/

Note: `cs-instance-watcher` reacts to the presence of 2 tags on the instance where it is deployed:
* `clonesquad:excluded` : When set to 'True', the 'new-connection-to-port-blocking' algorithm will immediatly blacklist configured ports.
* `clonesquad:force-excluded-instance-in-targetgroups` : When set to 'True', it disables the behavior associated with `clonesquad:excluded`.

**IAM Policy:**

The `cs-instance-watcher` tool requires the following IAM Policy in the EC2 attached role.

	{
	    "Version": "2012-10-17",
	    "Statement": [
	        {
	            "Sid": "csinstancewatcherlambda",
	            "Effect": "Allow",
	            "Action": "lambda:InvokeFunction",
	            "Resource": "arn:aws:lambda:*:*:function:CloneSquad-Discovery-*"
	        },
	        {
	            "Sid": "csinstancewatcherservices",
	            "Effect": "Allow",
	            "Action": [
	                "ec2:DescribeInstances",
	                "sts:GetCallerIdentity"
	            ],
	            "Resource": "*"
	        }
	    ]
	}



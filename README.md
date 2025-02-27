# MetaLore: Learning to Orchestrate the Metaverse

Metalore simulator has been developed to replicate a sub-metaverse environment for a smart city scenario. It is built upon the `mobile-env` platform, extending its functionality for adaptive communication and computation resource orchestration in smart city environments.

This simulator is developed as part of a Master's and PhD research project to explore dynamic resource allocation in smart city networks. By using RL, the system learns the optimal control policy, dynamically splitting the available cell bandwidth and computational resources between two groups: sensors and mobile devices. 
 
 A key contribution of the project is the novel delay modeling method integrated into the RL reward function that prioritizes synchronization between the digital and physical worlds. It leverages the age of information (AoI) to enhance user interaction quality and ensure continuity between physical and digital counterparts.

<center>
  <img src="Metalore_SS.png" alt="Description" width="400">
</center>


## Installation

### From Source (Development)

For development, you can clone `Metalore` from GitHub and install it from source.
After cloning, install in "editable" mode (-e):

```bash
pip install -e .
```

This is equivalent to running `pip install -r requirements.txt`.

## Example Usage

```python
import gymnasium
import mobile_env

env = gymnasium.make("mobile-smart_city-smart_city_handler-v0")
obs, info = env.reset()
done = False

while not done:
    action = ...
    obs, reward, terminated, truncated, info = env.step(action)
    done = terminated or truncated
    env.render()
```

## Documentation and API

Coming soon!

## Contributing

### Development Team: 
- [@elifohri](https://github.com/elifohri)
- [@bazziamir24](https://github.com/bazziamir24)

We welcome any contributions to the MetaLore Simulator. It can be adding new features, refining existing functionalities, resolving bugs or improving documentation.

### Citation:

f you use `MetaLore` simulator in your work, please cite our paper: coming soon!

### How to contribute:

1. Fork the Repository: Start by creating a fork of this repository to your GitHub account.
2. Create a Feature Branch: Work on your changes in a dedicated feature branch to keep development organized.
3. Submit a Pull Request (PR): Once your changes are ready, submit a PR describing the enhancement, fix or addition.

We value well-documented and tested contributions that align with the project's goals and coding standards.

### Feature Your Project

If you use MetaLore Simulator in your research, please let us know and we will feature your project. For any questions, feedback or ideas feel free to open an issue.

## Acknowledgements

MetaLore is a collaborative project between the LIP6 lab at Sorbonne University and Nokia Germany.

This project was developed using the `mobile-env` codebase. We extend our gratitude to the `mobile-env` team for their foundational work in mobile network simulation, which served as an important starting point for this project.
If you'd like to reference the original work, please see their [paper in PDF](https://ris.uni-paderborn.de/download/30236/30237/author_version.pdf).

For more information on mobile-env, visit their [GitHub repository](https://github.com/stefanbschneider/mobile-env).

For questions or further information, please feel free to contact elif-ebru.ohri@lip6.fr or open an issue on this repository.

## References
* S. Schneider, S. Werner, R. Khalili, A. Hecker, and H. Karl, “mobile-env: An open platform for reinforcement learning in wireless mobile networks,” in Network Operations and Management Symposium (NOMS). IEEE/IFIP, 2022.
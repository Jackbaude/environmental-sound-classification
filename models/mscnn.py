import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio
import numpy as np
from tqdm import tqdm
import logging
import os

from utils.model_utils import extract_features

logger = logging.getLogger(__name__)

class MultiScaleBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.conv3 = nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1)
        self.conv5 = nn.Conv2d(in_channels, out_channels, kernel_size=5, padding=2)
        self.conv7 = nn.Conv2d(in_channels, out_channels, kernel_size=7, padding=3)
        self.bn = nn.BatchNorm2d(out_channels * 3)
        self.relu = nn.ReLU()

    def forward(self, x):
        out3 = self.conv3(x)
        out5 = self.conv5(x)
        out7 = self.conv7(x)
        out = torch.cat([out3, out5, out7], dim=1)
        out = self.bn(out)
        out = self.relu(out)
        return out

class MSCNN(nn.Module):
    def __init__(self, num_classes=50):
        super().__init__()
        
        # Constants for audio processing
        self.SAMPLE_RATE = 44100
        self.N_MELS = 40  # As per paper
        self.N_FFT = 1024  # As per paper
        self.HOP_LENGTH = 512  # 50% overlap as per paper
        
        # Create mel spectrogram transform
        self.mel_transform = torchaudio.transforms.MelSpectrogram(
            sample_rate=self.SAMPLE_RATE,
            n_fft=self.N_FFT,
            hop_length=self.HOP_LENGTH,
            n_mels=self.N_MELS
        )
        
        # Multi-scale blocks
        self.block1 = MultiScaleBlock(1, 32)  # Input: 1 channel, Output: 96 channels (32*3)
        self.block2 = MultiScaleBlock(96, 64)  # Input: 96 channels, Output: 192 channels (64*3)
        self.block3 = MultiScaleBlock(192, 128)  # Input: 192 channels, Output: 384 channels (128*3)
        
        # Final convolution to reduce channels
        self.conv_final = nn.Conv2d(384, 128, kernel_size=1)
        self.bn_final = nn.BatchNorm2d(128)
        
        # Global average pooling
        self.global_pool = nn.AdaptiveAvgPool2d(1)
        
        # Fully connected layers
        self.fc1 = nn.Linear(128, 256)
        self.dropout = nn.Dropout(0.5)
        self.fc2 = nn.Linear(256, num_classes)

    def forward(self, x):
        # Ensure input is 4D (batch, channel, height, width)
        if x.dim() == 3:
            x = x.unsqueeze(1)
            
        # Multi-scale blocks
        x = self.block1(x)
        x = F.max_pool2d(x, 2)
        
        x = self.block2(x)
        x = F.max_pool2d(x, 2)
        
        x = self.block3(x)
        x = F.max_pool2d(x, 2)
        
        # Final convolution and pooling
        x = F.relu(self.bn_final(self.conv_final(x)))
        x = self.global_pool(x).squeeze(-1).squeeze(-1)
        
        # Fully connected layers
        x = F.relu(self.fc1(x))
        x = self.dropout(x)
        out = self.fc2(x)
        
        return out
        
    def fit(self, dataset, device, num_epochs=100, batch_size=32, learning_rate=0.001):
        # Get the original dataset from the Subset
        original_dataset = dataset.dataset
        
        # Initialize optimizer and loss function
        optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        criterion = nn.CrossEntropyLoss()
        
        # Move model to device
        self.to(device)
        
        # Training loop
        train_losses = []
        for epoch in range(num_epochs):
            self.train()
            epoch_loss = 0.0
            
            # Shuffle indices for this epoch
            indices = np.random.permutation(dataset.indices)
            
            # Process in batches
            for i in tqdm(range(0, len(indices), batch_size), desc=f'Epoch {epoch+1}/{num_epochs}'):
                batch_indices = indices[i:i+batch_size]
                
                # Get batch data
                batch_features = []
                batch_labels = []
                
                for idx in batch_indices:
                    audio_path = os.path.join(original_dataset.audio_dir, original_dataset.meta_data.iloc[idx]['filename'])
                    features = extract_features(
                        audio_path=audio_path,
                        mel_transform=self.mel_transform,
                        sample_rate=self.SAMPLE_RATE,
                        device=device
                    )
                    batch_features.append(features)
                    batch_labels.append(original_dataset.meta_data.iloc[idx]['target'])
                
                # Stack features and convert to tensor
                batch_features = torch.stack(batch_features)
                batch_labels = torch.tensor(batch_labels, device=device)
                
                # Forward pass
                optimizer.zero_grad()
                outputs = self(batch_features)
                loss = criterion(outputs, batch_labels)
                
                # Backward pass
                loss.backward()
                optimizer.step()
                
                epoch_loss += loss.item()
            
            # Calculate average loss for the epoch
            avg_loss = epoch_loss / (len(indices) // batch_size)
            train_losses.append(avg_loss)
            logger.info(f'Epoch {epoch+1}/{num_epochs}, Loss: {avg_loss:.4f}')
            
        return train_losses
        
    def predict(self, dataset, device):
        self.eval()
        predictions = []
        
        with torch.no_grad():
            for idx in tqdm(dataset.indices, desc='Predicting'):
                audio_path = os.path.join(dataset.dataset.audio_dir, dataset.dataset.meta_data.iloc[idx]['filename'])
                features = extract_features(
                    audio_path=audio_path,
                    mel_transform=self.mel_transform,
                    sample_rate=self.SAMPLE_RATE,
                    device=device
                )
                features = features.unsqueeze(0)  # Add batch dimension
                outputs = self(features)
                pred = torch.argmax(outputs, dim=1)
                predictions.append(pred.item())
                
        return np.array(predictions)
    
    def predict_proba(self, dataset, device):
        self.eval()
        probabilities = []
        
        with torch.no_grad():
            for idx in tqdm(dataset.indices, desc='Predicting probabilities'):
                audio_path = os.path.join(dataset.dataset.audio_dir, dataset.dataset.meta_data.iloc[idx]['filename'])
                features = extract_features(
                    audio_path=audio_path,
                    mel_transform=self.mel_transform,
                    sample_rate=self.SAMPLE_RATE,
                    device=device
                )
                features = features.unsqueeze(0)  # Add batch dimension
                outputs = self(features)
                probs = F.softmax(outputs, dim=1)
                probabilities.append(probs.cpu().numpy())
                
        return np.concatenate(probabilities, axis=0) 